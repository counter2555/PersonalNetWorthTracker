"""Web service: data entry, column configuration, Excel import/export and the dashboard."""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import threading
from pathlib import Path
from urllib.parse import quote, urlparse

import plotly
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from plotly.offline import get_plotlyjs
from starlette.middleware.sessions import SessionMiddleware

from . import achievements, dashboard, income_dashboard
from . import formula as fx
from .auth import check_credentials, ensure_configured, is_authenticated
from .columns import DATE_COLUMN, FORMATS, KINDS, ROLES, Column, Layout, form_sections
from .config import Config
from .db import Cell, Entry, Income, Store
from .excel import export_workbook, import_workbook
from .sheet import compute, to_frame

HERE = Path(__file__).parent
PUBLIC_PATHS = {"/login", "/healthz", "/static/style.css"}

PAGE_SIZES = (25, 50, 100)  # entries per page; 0 = all
DEFAULT_PAGE_SIZE = 50

NAV = [("/", "Dashboard"), ("/income/dashboard", "Einkommens-Dashboard"), ("/achievements", "Erfolge"),
       ("/entries", "Einträge"), ("/entries/new", "+ Neuer Eintrag"), ("/income", "Einkommen"),
       ("/columns", "Spalten"), ("/settings", "Einstellungen"), ("/export.xlsx", "Excel ⬇")]


# ---------------------------------------------------------------------------
# Formatting helpers (templates)
# ---------------------------------------------------------------------------

def _de(value: float, digits: int = 2) -> str:
    return dashboard._de(value, f",.{digits}f")


def format_value(value, number_format: str = "currency") -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    if number_format == "percent":
        return f"{_de(value * 100)} %"
    if number_format == "currency":
        return f"{_de(value)} €"
    return _de(value)


def format_input(cell: Cell | None, keep_expr: bool = True) -> str:
    """Value for an input field: the typed expression, else the number with a decimal comma."""
    if cell is None or cell.value is None:
        return ""
    if keep_expr and cell.expr:
        return cell.expr
    return f"{cell.value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def format_date(value: dt.date) -> str:
    return value.strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app(config: Config | None = None, store: Store | None = None) -> FastAPI:
    config = config or Config.from_env()
    ensure_configured(config)
    store = store or Store(config.database)

    app = FastAPI(title="Net Worth Tracker", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config, app.state.store = config, store
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(value=format_value, date=format_date, input=format_input)
    templates.env.globals.update(NAV=NAV, KINDS=KINDS, FORMATS=FORMATS, ROLES=ROLES, DATE_COLUMN=DATE_COLUMN,
                                 auth_enabled=not config.auth_disabled, THEME_CSS=dashboard.THEME_CSS,
                                 THEME_INIT_JS=dashboard.THEME_INIT_JS, THEME_SWITCH_JS=dashboard.THEME_SWITCH_JS,
                                 THEME_SWITCH_HTML=dashboard.THEME_SWITCH_HTML)

    cache: dict[str, tuple[int, str]] = {}
    cache_lock = threading.Lock()

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        if path in PUBLIC_PATHS or is_authenticated(config, request):
            return await call_next(request)
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"{_root(request)}/login?next={quote(path)}", status_code=303)
        return Response("Anmeldung erforderlich", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Net Worth"'})

    # added last = runs first, so the auth middleware sees the session
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key, session_cookie="networth_session",
                       max_age=config.session_days * 86400, same_site="lax", https_only=config.secure_cookies)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        flashes = request.session.pop("flash", []) if "session" in request.scope else []
        return templates.TemplateResponse(request, name, {"root": _root(request), "flashes": flashes,
                                                          "path": request.url.path, **context},
                                          status_code=status_code)

    def redirect(request: Request, path: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
        if message:
            request.session.setdefault("flash", []).append([kind, message])
        return RedirectResponse(_root(request) + path, status_code=303)

    def sheet():
        columns = store.columns()
        entries = store.entries()
        return columns, entries, compute(columns, entries)

    # -- basics -------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return page(request, "login.html", next=_safe_next(next))

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        target = _safe_next(str(form.get("next", "/")))
        if check_credentials(config, str(form.get("username", "")), str(form.get("password", ""))):
            request.session.clear()
            request.session["user"] = config.username
            return RedirectResponse(_root(request) + target, status_code=303)
        await asyncio.sleep(1)  # slow down guessing
        return page(request, "login.html", status_code=401, next=target, error="Benutzername oder Passwort falsch")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(_root(request) + "/login", status_code=303)

    @app.get("/assets/plotly.min.js")
    def plotly_js():
        return Response(get_plotlyjs(), media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # -- dashboard ----------------------------------------------------------

    def cached(request: Request, name: str, standalone: bool, build) -> str | None:
        """Dashboard HTML from ``build(plotly_src, nav)``, cached until the data changes."""
        root = _root(request)
        key = f"{name}:standalone" if standalone else f"{name}:page:{root}"
        revision = store.revision()
        with cache_lock:
            if key in cache and cache[key][0] == revision:
                return cache[key][1]
        nav = "" if standalone else _nav_html(root, request.url.path.removeprefix(root),
                                              logout=not config.auth_disabled)
        plotly_src = None if standalone else f"{root}/assets/plotly.min.js?v={plotly.__version__}"
        content = build(plotly_src, nav)
        with cache_lock:
            cache[key] = (revision, content)
        return content

    def render_dashboard(request: Request, standalone: bool) -> str | None:
        def build(plotly_src: str | None, nav: str) -> str | None:
            columns, entries, rows = sheet()
            if not rows:
                return None
            settings = store.settings()
            layout = Layout.from_columns(columns, store.group_order())
            income = dashboard.load_income([(i.date, i.amount) for i in store.income()])
            return dashboard.render(to_frame(columns, rows), layout, income, float(settings["target"]),
                                    show_distribution=settings.get("show_distribution", "1") == "1",
                                    plotly_src=plotly_src, nav=nav)
        return cached(request, "networth", standalone, build)

    def render_income_dashboard(request: Request, standalone: bool) -> str | None:
        def build(plotly_src: str | None, nav: str) -> str | None:
            income = dashboard.load_income([(i.date, i.amount) for i in store.income()])
            if income is None:
                return None
            columns, entries, rows = sheet()
            layout = Layout.from_columns(columns, store.group_order())
            return income_dashboard.render(income, to_frame(columns, rows) if rows else None, layout,
                                           plotly_src=plotly_src, nav=nav)
        return cached(request, "income", standalone, build)

    def render_achievements(request: Request) -> str | None:
        def build(plotly_src: str | None, nav: str) -> str | None:
            columns, entries, rows = sheet()
            income = dashboard.load_income([(i.date, i.amount) for i in store.income()])
            if not rows and income is None:
                return None
            layout = Layout.from_columns(columns, store.group_order())
            return achievements.render(to_frame(columns, rows) if rows else None, layout, income,
                                       float(store.settings()["target"]), nav=nav)
        return cached(request, "achievements", False, build)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        try:
            content = render_dashboard(request, standalone=False)
        except Exception as exc:  # a broken column configuration must not lock the user out of the UI
            return page(request, "message.html", status_code=500, title="Dashboard-Fehler",
                        message=f"Das Dashboard konnte nicht erstellt werden: {exc}. "
                                "Prüfe die Rollen und Anlagegruppen unter „Spalten“.")
        if content is None:
            return page(request, "message.html", title="Noch keine Daten",
                        message="Lege den ersten Eintrag an oder importiere dein Excel unter „Einstellungen“.")
        return HTMLResponse(content)

    @app.get("/dashboard.html")
    def dashboard_download(request: Request):
        content = render_dashboard(request, standalone=True)
        if content is None:
            raise HTTPException(404, "Noch keine Daten")
        return HTMLResponse(content, headers={"Content-Disposition": _attachment("net_worth_dashboard", "html")})

    @app.get("/income/dashboard", response_class=HTMLResponse)
    def income_dashboard_page(request: Request):
        try:
            content = render_income_dashboard(request, standalone=False)
        except Exception as exc:  # same as the net worth dashboard: never lock the user out of the UI
            return page(request, "message.html", status_code=500, title="Dashboard-Fehler",
                        message=f"Das Einkommens-Dashboard konnte nicht erstellt werden: {exc}.")
        if content is None:
            return page(request, "message.html", title="Noch kein Einkommen",
                        message="Trage dein Nettogehalt unter „Einkommen“ ein oder importiere dein Excel "
                                "(Sheet „Income“) unter „Einstellungen“.")
        return HTMLResponse(content)

    @app.get("/income-dashboard.html")
    def income_dashboard_download(request: Request):
        content = render_income_dashboard(request, standalone=True)
        if content is None:
            raise HTTPException(404, "Noch kein Einkommen")
        return HTMLResponse(content, headers={"Content-Disposition": _attachment("income_dashboard", "html")})

    @app.get("/achievements", response_class=HTMLResponse)
    def achievements_page(request: Request):
        try:
            content = render_achievements(request)
        except Exception as exc:  # same as the dashboards: never lock the user out of the UI
            return page(request, "message.html", status_code=500, title="Erfolge-Fehler",
                        message=f"Die Erfolge konnten nicht berechnet werden: {exc}.")
        if content is None:
            return page(request, "message.html", title="Noch keine Erfolge",
                        message="Lege den ersten Vermögens-Eintrag an oder trage dein Nettogehalt unter "
                                "„Einkommen“ ein – dann gibt es hier Achievements.")
        return HTMLResponse(content)

    # -- entries ------------------------------------------------------------

    @app.get("/entries", response_class=HTMLResponse)
    def entries_list(request: Request, page_no: int = Query(1, alias="page"), per: int = DEFAULT_PAGE_SIZE):
        columns, entries, rows = sheet()
        rows = list(reversed(rows))  # newest first
        per = per if per in (*PAGE_SIZES, 0) else DEFAULT_PAGE_SIZE
        pages = max(1, -(-len(rows) // per)) if per else 1
        page_no = min(max(page_no, 1), pages)
        start = (page_no - 1) * per if per else 0
        shown = rows[start:start + per] if per else rows
        return page(request, "entries.html", columns=columns, rows=shown, total=len(rows), page_no=page_no,
                    pages=pages, per=per, first=start + 1, page_sizes=PAGE_SIZES)

    def entry_form(request: Request, entry: Entry, columns: list[Column], errors: dict[str, str] | None = None,
                   raw: dict[str, str] | None = None, status_code: int = 200, is_new: bool = False):
        computed = None
        if entry.id is not None:
            _, entries, rows = sheet()
            computed = next((r for r in rows if r.entry_id == entry.id), None)
        fixed = [c for c in columns if c.kind == "formula" and c.id in entry.cells and entry.cells[c.id].override]
        return page(request, "entry_form.html", status_code=status_code, entry=entry, columns=columns,
                    sections=form_sections(columns), errors=errors or {}, raw=raw or {}, computed=computed,
                    is_new=is_new, fixed=fixed)

    @app.get("/entries/new", response_class=HTMLResponse)
    def entry_new(request: Request):
        columns, entries = store.columns(), store.entries()
        previous = entries[-1] if entries else None
        cells = {}
        if previous is not None:  # most balances don't change: start from the last entry
            cells = {cid: Cell(value=c.value) for cid, c in previous.cells.items()
                     if not c.override and any(col.id == cid and col.kind == "input" for col in columns)}
        return entry_form(request, Entry(date=dt.date.today(), cells=cells), columns, is_new=True)

    async def save_entry_from_form(request: Request, entry_id: int | None):
        form = await request.form()
        columns = store.columns()
        existing = store.entry(entry_id) if entry_id is not None else None
        if entry_id is not None and existing is None:
            raise HTTPException(404)
        raw = {k: str(v) for k, v in form.items()}
        try:
            date = dt.date.fromisoformat(raw.get("date", ""))
        except ValueError:
            date = None
        cells: dict[int, Cell] = {}
        errors = {} if date else {"date": "Bitte ein gültiges Datum angeben"}  # form field -> message
        for col in columns:
            key = f"c{col.id}"
            if col.kind == "input":
                try:
                    value, expr = fx.parse_number_input(raw.get(key, ""))
                except fx.FormulaError as exc:
                    errors[key] = str(exc)
                    continue
                if value is not None:
                    cells[col.id] = Cell(value=value, expr=expr)
            elif col.kind == "text":
                text = raw.get(key, "").strip()
                if text:
                    cells[col.id] = Cell(text=text)
            elif existing is not None and col.id in existing.cells and existing.cells[col.id].override:
                if f"reset{col.id}" not in raw:  # keep the fixed value unless the user resets it to the formula
                    cells[col.id] = existing.cells[col.id]
        entry = Entry(date=date or dt.date.today(), cells=cells, id=entry_id)
        if errors:
            return entry_form(request, existing or entry, columns, errors=errors, raw=raw,
                              status_code=422, is_new=entry_id is None)
        store.save_entry(entry)
        return redirect(request, "/entries", f"Eintrag vom {format_date(entry.date)} gespeichert")

    @app.post("/entries/new")
    async def entry_create(request: Request):
        return await save_entry_from_form(request, None)

    @app.get("/entries/{entry_id}", response_class=HTMLResponse)
    def entry_edit(request: Request, entry_id: int):
        entry = store.entry(entry_id)
        if entry is None:
            raise HTTPException(404)
        return entry_form(request, entry, store.columns())

    @app.post("/entries/{entry_id}")
    async def entry_update(request: Request, entry_id: int):
        return await save_entry_from_form(request, entry_id)

    @app.post("/entries/{entry_id}/delete")
    def entry_delete(request: Request, entry_id: int):
        store.delete_entry(entry_id)
        return redirect(request, "/entries", "Eintrag gelöscht")

    # -- income -------------------------------------------------------------

    @app.get("/income", response_class=HTMLResponse)
    def income_list(request: Request):
        items = list(reversed(store.income()))
        years: dict[int, float] = {}
        for item in items:
            years[item.date.year] = years.get(item.date.year, 0.0) + item.amount
        return page(request, "income.html", items=items, years=years, today=dt.date.today())

    @app.post("/income")
    async def income_save(request: Request):
        form = await request.form()
        try:
            date = dt.date.fromisoformat(str(form.get("date", "")))
            amount, _ = fx.parse_number_input(str(form.get("amount", "")))
            if amount is None:
                raise fx.FormulaError("Betrag fehlt")
        except (ValueError, fx.FormulaError) as exc:
            return redirect(request, "/income", f"Nicht gespeichert: {exc}", "error")
        income_id = form.get("id")
        store.save_income(Income(date, amount, int(income_id) if income_id else None))
        return redirect(request, "/income", "Einkommen gespeichert")

    @app.post("/income/{income_id}/delete")
    def income_delete(request: Request, income_id: int):
        store.delete_income(income_id)
        return redirect(request, "/income", "Eintrag gelöscht")

    # -- columns ------------------------------------------------------------

    @app.get("/columns", response_class=HTMLResponse)
    def columns_list(request: Request):
        return page(request, "columns.html", columns=store.columns())

    def column_form(request: Request, col: Column, error: str | None = None, status_code: int = 200):
        columns = store.columns()
        groups = sorted({c.asset_group for c in columns if c.asset_group} | set(store.group_order()))
        return page(request, "column_form.html", status_code=status_code, col=col, columns=columns,
                    groups=groups, error=error)

    @app.get("/columns/new", response_class=HTMLResponse)
    def column_new(request: Request):
        return column_form(request, Column(name=""))

    @app.get("/columns/{column_id}", response_class=HTMLResponse)
    def column_edit(request: Request, column_id: int):
        col = next((c for c in store.columns() if c.id == column_id), None)
        if col is None:
            raise HTTPException(404)
        return column_form(request, col)

    async def save_column_from_form(request: Request, column_id: int | None):
        form = {k: str(v).strip() for k, v in (await request.form()).items()}
        existing = next((c for c in store.columns() if c.id == column_id), None) if column_id else None
        try:
            width = float(fx.normalize_number_input(form.get("width", "") or "12"))
        except ValueError:
            width = 12.0
        col = Column(
            id=column_id,
            name=form.get("name", ""),
            kind=existing.kind if existing else form.get("kind", "input"),
            formula=form.get("formula", "") if (existing.kind if existing else form.get("kind")) == "formula" else "",
            number_format=form.get("number_format", "currency"),
            width=width,
            asset_group=form.get("asset_group", ""),
            role=form.get("role", ""),
            net_into=form.get("net_into", ""),
            highlight=form.get("highlight", ""),
        )
        try:
            store.save_column(col)
        except (ValueError, fx.FormulaError) as exc:
            return column_form(request, col, error=str(exc), status_code=422)
        return redirect(request, "/columns", f"Spalte „{col.name}“ gespeichert")

    @app.post("/columns/new")
    async def column_create(request: Request):
        return await save_column_from_form(request, None)

    @app.post("/columns/{column_id}")
    async def column_update(request: Request, column_id: int):
        return await save_column_from_form(request, column_id)

    @app.post("/columns/{column_id}/delete")
    def column_delete(request: Request, column_id: int):
        try:
            store.delete_column(column_id)
        except (ValueError, StopIteration) as exc:
            return redirect(request, "/columns", f"Nicht gelöscht: {exc}", "error")
        return redirect(request, "/columns", "Spalte gelöscht")

    @app.post("/columns/{column_id}/move")
    async def column_move(request: Request, column_id: int):
        form = await request.form()
        try:
            store.move_column(column_id, int(str(form.get("offset", "0"))))
        except ValueError as exc:
            return redirect(request, "/columns", f"Nicht verschoben: {exc}", "error")
        return redirect(request, "/columns")

    # -- settings, import, export ------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(request, "settings.html", settings=store.settings(), entry_count=len(store.entries()))

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        try:
            target, _ = fx.parse_number_input(str(form.get("target", "")))
            if not target or target <= 0:
                raise fx.FormulaError("Ziel muss größer als 0 sein")
        except fx.FormulaError as exc:
            return redirect(request, "/settings", f"Nicht gespeichert: {exc}", "error")
        groups = ", ".join(g.strip() for g in str(form.get("group_order", "")).split(",") if g.strip())
        store.set_settings({"target": str(target), "group_order": groups,
                            "show_distribution": "1" if form.get("show_distribution") else "0"})
        return redirect(request, "/settings", "Einstellungen gespeichert")

    @app.post("/import")
    async def import_excel(request: Request):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return redirect(request, "/settings", "Keine Datei ausgewählt", "error")
        if form.get("confirm") != "yes":
            return redirect(request, "/settings", "Bitte bestätigen, dass die bestehenden Einträge ersetzt werden",
                            "error")
        try:
            result = import_workbook(await upload.read(), store.columns())
            store.replace_all(result.new_columns, result.entries, result.income)
        except Exception as exc:  # any unreadable workbook ends up here
            return redirect(request, "/settings", f"Import fehlgeschlagen: {exc}", "error")
        parts = [f"{len(result.entries)} Einträge importiert"]
        if result.income is not None:
            parts.append(f"{len(result.income)} Einkommens-Einträge")
        if result.new_columns:
            parts.append("neue Spalten: " + ", ".join(c.name for c in result.new_columns))
        if result.fixed_values:
            parts.append(f"{result.fixed_values} Formelzellen weichen von der aktuellen Formel ab und wurden als "
                         "Festwert übernommen")
        if result.ignored:
            parts.append("leere Spalten ignoriert: " + ", ".join(result.ignored))
        return redirect(request, "/settings", "; ".join(parts))

    @app.get("/export.xlsx")
    def export_excel():
        columns, entries, rows = sheet()
        content = export_workbook(columns, entries, rows, store.income())
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": _attachment("NetWorth", "xlsx")})

    return app


def _root(request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/")


def _safe_next(target: str) -> str:
    """Only allow redirects within this service."""
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _attachment(stem: str, extension: str) -> str:
    return f'attachment; filename="{stem}_{dt.date.today():%Y-%m-%d}.{extension}"'


def _nav_html(root: str, active: str, logout: bool) -> str:
    links = "".join(f'<a href="{html.escape(root + href)}"{" class=active" if href == active else ""}>'
                    f"{html.escape(label)}</a>" for href, label in NAV)
    if logout:
        links += (f'<form method="post" action="{html.escape(root)}/logout" class="logout">'
                  '<button type="submit">Abmelden</button></form>')
    return f"<nav>{links}</nav>"
