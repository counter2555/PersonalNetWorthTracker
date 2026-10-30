"""Web service: data entry, accounts and metrics, Excel import/export and the dashboard."""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, urlparse

import plotly
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from plotly.offline import get_plotlyjs
from starlette.middleware.sessions import SessionMiddleware

from . import achievements, dashboard, income_dashboard
from . import formula as fx
from .auth import current_user, ensure_configured
from .columns import DATE_COLUMN, FORMATS, Column, Layout, form_sections
from .config import Config
from .db import Cell, Entry, Income, Store
from .excel import export_workbook, import_workbook
from .sheet import compute, to_frame
from .structure import (ACCOUNT_SPECS, AUTOMATIC, DEFAULT_NAMES, DEPOT_METRICS, KIND_OF, LAYOUTS, MEMBER_METRICS,
                        METRICS, OTHER_GROUP, SPECS, TEXT_SPECS, TOP_ACCOUNTS, member_candidates, member_formula,
                        members,
                        overview, spec_of)
from .users import User, UserError, Users

HERE = Path(__file__).parent
PUBLIC_PATHS = {"/login", "/register", "/healthz", "/static/style.css"}
SIGN_IN_PATHS = {"/login", "/register", "/logout"}

PAGE_SIZES = (25, 50, 100)  # entries per page; 0 = all
DEFAULT_PAGE_SIZE = 50

# menu groups, shown with a divider in between: evaluations, data, administration
NAV = [[("/", "Dashboard"), ("/income/dashboard", "Einkommens-Dashboard"), ("/achievements", "Erfolge")],
       [("/entries", "Einträge"), ("/income", "Einkommen"), ("/columns", "Konten & Kennzahlen")],
       [("/settings", "Einstellungen"), ("/export.xlsx", "Excel ⬇")]]
ADMIN_NAV = [("/admin/users", "Benutzer")]
NEW_ENTRY = ("/entries/new", "+ Neuer Eintrag")  # highlighted as the main action
NEW_GROUP = "__new__"  # account form: option "new group"


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


def format_rate(value: float) -> str:
    """Tax rate for an input field: 0.275 -> "27,5"."""
    return f"{value * 100:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _width(form: dict[str, str]) -> float:
    """Excel column width from a form; empty = automatic (0)."""
    try:
        return float(fx.normalize_number_input(form.get("width", "") or "0"))
    except ValueError:
        return 0.0


def _rate(text: str) -> float:
    value, _ = fx.parse_number_input(text.replace("%", ""))
    if value is None or not 0 <= value < 100:
        raise ValueError("Steuersatz in Prozent angeben, z. B. 27,5")
    return value / 100


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app(config: Config | None = None, users: Users | None = None) -> FastAPI:
    config = config or Config.from_env()
    users = users or Users(config.data_dir)
    ensure_configured(config, users)

    async def same_user(request: Request) -> None:
        """Refuse forms that were shown to another user of this browser.

        All tabs of a browser share the session cookie: after logging in as someone else in a second tab, a form
        still open in the first tab would otherwise change the data of the new user. Pages add the user id they were
        rendered for to every form (``as_user``, see base.html); scripts without it are not affected.
        """
        if request.method != "POST" or _path(request) in SIGN_IN_PATHS:
            return
        user = getattr(request.state, "user", None)
        expected = (await request.form()).get("as_user")  # cached, the endpoint reads the same form
        if user is not None and expected is not None and str(expected) != str(user.id):
            raise UserSwitched(user.username)

    app = FastAPI(title="Net Worth Tracker", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(same_user)])

    @app.exception_handler(UserSwitched)
    async def user_switched(request: Request, exc: UserSwitched):
        return redirect(request, "/", f"Nicht gespeichert: In diesem Browser ist inzwischen „{exc.username}“ "
                                      "angemeldet (z. B. in einem anderen Tab). Die Seite zeigt jetzt dessen Daten.",
                        "error")
    app.state.config, app.state.users = config, users
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(value=format_value, date=format_date, input=format_input, rate=format_rate)
    templates.env.globals.update(LAYOUTS=LAYOUTS, FORMATS=FORMATS, SPECS=SPECS, ACCOUNT_SPECS=ACCOUNT_SPECS,
                                 METRICS=METRICS, NEW_GROUP=NEW_GROUP, DATE_COLUMN=DATE_COLUMN,
                                 auth_enabled=not config.auth_disabled,
                                 THEME_CSS=dashboard.THEME_CSS + dashboard.TOOLBAR_CSS,
                                 THEME_INIT_JS=dashboard.THEME_INIT_JS, THEME_SWITCH_JS=dashboard.THEME_SWITCH_JS)

    cache: dict[str, tuple[int, str]] = {}
    cache_lock = threading.Lock()

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = _path(request)
        user = await asyncio.to_thread(current_user, config, users, request)  # Basic auth hashes the password
        request.state.user = user
        if user is not None or path in PUBLIC_PATHS:
            if path.startswith("/admin/") and not (user and user.is_admin):
                return Response("Nur für Administratoren", status_code=403)
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
        user = getattr(request.state, "user", None)
        root = _root(request)
        nav = "" if user is None else _nav_html(root, request.url.path.removeprefix(root), user,
                                                logout=not config.auth_disabled)
        return templates.TemplateResponse(request, name, {"root": root, "flashes": flashes,
                                                          "path": request.url.path, "user": user,
                                                          "toolbar": dashboard.toolbar_html(nav), **context},
                                          status_code=status_code)

    def data(request: Request) -> Store:
        """The data of the logged in user."""
        return users.store(request.state.user.id)

    def redirect(request: Request, path: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
        if message:
            request.session.setdefault("flash", []).append([kind, message])
        return RedirectResponse(_root(request) + path, status_code=303)

    def sheet(request: Request):
        columns = data(request).columns()
        entries = data(request).entries()
        return columns, entries, compute(columns, entries)

    # -- basics -------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return page(request, "login.html", next=_safe_next(next), registration=registration_open())

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        target = _safe_next(str(form.get("next", "/")))
        user = await asyncio.to_thread(users.authenticate, str(form.get("username", "")),
                                       str(form.get("password", "")))
        if user is not None:
            sign_in(request, user)
            return RedirectResponse(_root(request) + target, status_code=303)
        await asyncio.sleep(1)  # slow down guessing
        return page(request, "login.html", status_code=401, next=target, error="Benutzername oder Passwort falsch",
                    registration=registration_open())

    def sign_in(request: Request, user: User) -> None:
        request.session.clear()
        request.session["uid"] = user.id
        users.touch_login(user.id)

    def registration_open() -> bool:
        return not config.auth_disabled and users.registration_open()

    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request):
        if not registration_open():
            return page(request, "register.html", status_code=403, closed=True)
        return page(request, "register.html")

    @app.post("/register")
    async def register(request: Request):
        if not registration_open():
            return page(request, "register.html", status_code=403, closed=True)
        form = {k: str(v) for k, v in (await request.form()).items()}
        username, password = form.get("username", "").strip(), form.get("password", "")
        try:
            if password != form.get("password2", ""):
                raise UserError("Die Passwörter stimmen nicht überein")
            user = await asyncio.to_thread(users.create, username, password)
        except UserError as exc:
            return page(request, "register.html", status_code=422, error=str(exc), username=username)
        sign_in(request, user)
        return redirect(request, "/", f"Willkommen, {user.username}! Dein Konto wurde angelegt.")

    @app.get("/whoami")
    def whoami(request: Request):
        """Lets open pages notice a login as another user in a different tab (base.html reloads them)."""
        return {"id": request.state.user.id, "username": request.state.user.username}

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
        user = request.state.user
        key = f"{user.id}:{name}:standalone" if standalone else f"{user.id}:{name}:page:{root}:{user.is_admin}"
        revision = data(request).revision()
        with cache_lock:
            if key in cache and cache[key][0] == revision:
                return cache[key][1]
        nav = "" if standalone else _nav_html(root, request.url.path.removeprefix(root), user,
                                              logout=not config.auth_disabled)
        plotly_src = None if standalone else f"{root}/assets/plotly.min.js?v={plotly.__version__}"
        content = build(plotly_src, nav)
        with cache_lock:
            cache[key] = (revision, content)
        return content

    def render_dashboard(request: Request, standalone: bool) -> str | None:
        def build(plotly_src: str | None, nav: str) -> str | None:
            columns, entries, rows = sheet(request)
            if not rows:
                return None
            settings = data(request).settings()
            layout = Layout.from_columns(columns, data(request).group_order())
            income = dashboard.load_income([(i.date, i.amount) for i in data(request).income()])
            return dashboard.render(to_frame(columns, rows), layout, income, float(settings["target"]),
                                    show_distribution=settings.get("show_distribution", "1") == "1",
                                    plotly_src=plotly_src, nav=nav)
        return cached(request, "networth", standalone, build)

    def render_income_dashboard(request: Request, standalone: bool) -> str | None:
        def build(plotly_src: str | None, nav: str) -> str | None:
            income = dashboard.load_income([(i.date, i.amount) for i in data(request).income()])
            if income is None:
                return None
            columns, entries, rows = sheet(request)
            layout = Layout.from_columns(columns, data(request).group_order())
            return income_dashboard.render(income, to_frame(columns, rows) if rows else None, layout,
                                           plotly_src=plotly_src, nav=nav)
        return cached(request, "income", standalone, build)

    def render_achievements(request: Request) -> str | None:
        def build(plotly_src: str | None, nav: str) -> str | None:
            columns, entries, rows = sheet(request)
            income = dashboard.load_income([(i.date, i.amount) for i in data(request).income()])
            if not rows and income is None:
                return None
            layout = Layout.from_columns(columns, data(request).group_order())
            return achievements.render(to_frame(columns, rows) if rows else None, layout, income,
                                       float(data(request).settings()["target"]), nav=nav)
        return cached(request, "achievements", False, build)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        try:
            content = render_dashboard(request, standalone=False)
        except Exception as exc:  # a broken column configuration must not lock the user out of the UI
            return page(request, "message.html", status_code=500, title="Dashboard-Fehler",
                        message=f"Das Dashboard konnte nicht erstellt werden: {exc}. "
                                "Prüfe die Einstellungen unter „Konten & Kennzahlen“.")
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
        columns, entries, rows = sheet(request)
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
            _, entries, rows = sheet(request)
            computed = next((r for r in rows if r.entry_id == entry.id), None)
        fixed = [c for c in columns if c.kind == "formula" and c.id in entry.cells and entry.cells[c.id].override]
        return page(request, "entry_form.html", status_code=status_code, entry=entry, columns=columns,
                    sections=form_sections(columns), errors=errors or {}, raw=raw or {}, computed=computed,
                    is_new=is_new, fixed=fixed)

    @app.get("/entries/new", response_class=HTMLResponse)
    def entry_new(request: Request):
        columns, entries = data(request).columns(), data(request).entries()
        previous = entries[-1] if entries else None
        cells = {}
        if previous is not None:  # most balances don't change: start from the last entry
            cells = {cid: Cell(value=c.value) for cid, c in previous.cells.items()
                     if not c.override and any(col.id == cid and col.kind == "input" for col in columns)}
        return entry_form(request, Entry(date=dt.date.today(), cells=cells), columns, is_new=True)

    async def save_entry_from_form(request: Request, entry_id: int | None):
        form = await request.form()
        columns = data(request).columns()
        existing = data(request).entry(entry_id) if entry_id is not None else None
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
        data(request).save_entry(entry)
        return redirect(request, "/entries", f"Eintrag vom {format_date(entry.date)} gespeichert")

    @app.post("/entries/new")
    async def entry_create(request: Request):
        return await save_entry_from_form(request, None)

    @app.get("/entries/{entry_id}", response_class=HTMLResponse)
    def entry_edit(request: Request, entry_id: int):
        entry = data(request).entry(entry_id)
        if entry is None:
            raise HTTPException(404)
        return entry_form(request, entry, data(request).columns())

    @app.post("/entries/{entry_id}")
    async def entry_update(request: Request, entry_id: int):
        return await save_entry_from_form(request, entry_id)

    @app.post("/entries/{entry_id}/delete")
    def entry_delete(request: Request, entry_id: int):
        data(request).delete_entry(entry_id)
        return redirect(request, "/entries", "Eintrag gelöscht")

    # -- income -------------------------------------------------------------

    @app.get("/income", response_class=HTMLResponse)
    def income_list(request: Request):
        items = list(reversed(data(request).income()))
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
        data(request).save_income(Income(date, amount, int(income_id) if income_id else None))
        return redirect(request, "/income", "Einkommen gespeichert")

    @app.post("/income/{income_id}/delete")
    def income_delete(request: Request, income_id: int):
        data(request).delete_income(income_id)
        return redirect(request, "/income", "Eintrag gelöscht")

    # -- accounts, metrics and text fields ("columns") ---------------------

    @app.get("/columns", response_class=HTMLResponse)
    def columns_list(request: Request):
        store = data(request)
        return page(request, "columns.html", overview=overview(store.columns(), store.group_order()),
                    tax_rate=store.tax_rate(), notes=store.structure_notes())

    def column_form(request: Request, col: Column, error: str | None = None, status_code: int = 200,
                    gain_name: str = "", tax_rate: float | None = None):
        store = data(request)
        columns = store.columns()
        spec = spec_of(col)
        context = dict(col=col, spec=spec, columns=columns, error=error,
                       tax_rate=store.tax_rate() if tax_rate is None else tax_rate)
        if spec in ACCOUNT_SPECS:
            gain = next((c for c in columns if c.spec == "depot_gain"), None)
            depot = next((c for c in columns if c.spec == "depot"), None)
            return page(request, "account_form.html", status_code=status_code, groups=store.group_order(),
                        targets=[c for c in columns if c.spec in TOP_ACCOUNTS and c.id != col.id],
                        has_other_depot=depot is not None and depot.id != col.id,
                        gain_name=gain_name or (gain.name if gain else DEFAULT_NAMES["depot_gain"]), **context)
        if spec in TEXT_SPECS:
            return page(request, "field_form.html", status_code=status_code, **context)
        has_depot = any(c.spec == "depot" for c in columns)
        return page(request, "metric_form.html", status_code=status_code,
                    candidates=[c for c in member_candidates(columns) if c.id != col.id],
                    selected=set(members(col)) if spec in MEMBER_METRICS else set(),
                    metrics={k: v for k, v in METRICS.items() if has_depot or k not in DEPOT_METRICS},
                    references=[c for c in columns if c.is_number and c.id != col.id], **context)

    def find_column(request: Request, column_id: int) -> Column:
        col = next((c for c in data(request).columns() if c.id == column_id), None)
        if col is None:
            raise HTTPException(404)
        return col

    @app.get("/columns/new", response_class=HTMLResponse)
    def column_new(request: Request, type: str = "account", group: str = ""):
        if type == "metric":
            return column_form(request, Column(name="", kind="formula", spec="change_prev", width=0))
        if type == "note":
            return column_form(request, Column(name="", kind="text", spec="note", width=0))
        return column_form(request, Column(name="", spec="asset", asset_group=group, width=0))

    @app.get("/columns/{column_id}", response_class=HTMLResponse)
    def column_edit(request: Request, column_id: int):
        col = find_column(request, column_id)
        if col.spec == "depot_gain":  # edited together with the depot
            depot = next(c for c in data(request).columns() if c.spec == "depot")
            return RedirectResponse(f"{_root(request)}/columns/{depot.id}", status_code=303)
        return column_form(request, col)

    async def save_column_from_form(request: Request, column_id: int | None):
        form = await request.form()
        values = {k: str(v).strip() for k, v in form.items()}
        existing = find_column(request, column_id) if column_id else None
        spec = values.get("spec") or (spec_of(existing) if existing else "asset")
        if existing is not None and spec_of(existing) in AUTOMATIC | {"comment"}:
            spec = spec_of(existing)  # fixed fields keep their kind
        if spec not in SPECS or spec in AUTOMATIC | {"comment"} and existing is None:
            spec = "asset"
        base = existing or Column(name="")
        col = replace(base, name=values.get("name", ""), spec=spec, kind=KIND_OF[spec], width=_width(values),
                      highlight=values.get("highlight", base.highlight))
        tax_rate = gain_name = None
        if spec in ACCOUNT_SPECS:
            group = values.get("new_group", "") if values.get("group") == NEW_GROUP else values.get("group", "")
            if not group and spec in TOP_ACCOUNTS and values.get("group") != NEW_GROUP:  # e.g. scripts
                group = next(iter(data(request).group_order()), OTHER_GROUP)
            col = replace(col, asset_group=group, net_into=values.get("net_into", ""),
                          number_format=values.get("number_format", "currency") if spec == "value" else "currency")
            gain_name = values.get("gain_name", "") if spec == "depot" else None
        elif spec in MEMBER_METRICS:
            col = replace(col, formula=member_formula([str(m) for m in form.getlist("members")]))
        elif spec == "custom":
            col = replace(col, formula=values.get("formula", ""), number_format=values.get("number_format", "currency"))
        try:
            if spec in ("depot", "tax") and "tax_rate" in values:
                tax_rate = _rate(values["tax_rate"])
            saved = data(request).save_column(col, tax_rate=tax_rate)
            if gain_name:
                gain = next(c for c in data(request).columns() if c.spec == "depot_gain")
                if gain.name != gain_name:
                    data(request).save_column(replace(gain, name=gain_name))
        except (ValueError, fx.FormulaError) as exc:
            return column_form(request, col, error=str(exc), status_code=422, gain_name=gain_name or "",
                               tax_rate=tax_rate)
        return redirect(request, "/columns", f"„{saved.name}“ gespeichert")

    @app.post("/columns/new")
    async def column_create(request: Request):
        return await save_column_from_form(request, None)

    @app.post("/columns/reset")
    async def columns_reset(request: Request):
        form = await request.form()
        layout = str(form.get("layout", ""))
        if layout not in LAYOUTS or form.get("confirm") != "yes":
            return redirect(request, "/columns", "Bitte ein Layout wählen und das Löschen bestätigen", "error")
        removed = data(request).reset_columns(layout)
        return redirect(request, "/columns", "Zurückgesetzt" + (f"; gelöscht: {', '.join(removed)}" if removed else ""))

    @app.post("/columns/order")
    async def columns_order(request: Request):
        """Drag and drop: ``order`` is ``[[group, [account ids]], ...]`` in the new order."""
        form = await request.form()
        try:
            order = {str(group): [int(i) for i in ids] for group, ids in json.loads(str(form.get("order", "")))}
            data(request).order_accounts(order)
        except (ValueError, TypeError) as exc:
            return redirect(request, "/columns", f"Nicht verschoben: {exc}", "error")
        return redirect(request, "/columns")

    @app.post("/columns/notes/dismiss")
    def columns_notes_dismiss(request: Request):
        data(request).dismiss_structure_notes()
        return redirect(request, "/columns")

    @app.post("/columns/{column_id}")
    async def column_update(request: Request, column_id: int):
        return await save_column_from_form(request, column_id)

    @app.post("/columns/{column_id}/delete")
    def column_delete(request: Request, column_id: int):
        try:
            deleted = data(request).delete_column(column_id)
        except (ValueError, StopIteration) as exc:
            return redirect(request, "/columns", f"Nicht gelöscht: {exc}", "error")
        return redirect(request, "/columns", f"Gelöscht: {', '.join(deleted)}")

    @app.post("/columns/{column_id}/move")
    async def column_move(request: Request, column_id: int):
        form = await request.form()
        try:
            data(request).move_column(column_id, int(str(form.get("offset", "0"))))
        except (ValueError, StopIteration) as exc:
            return redirect(request, "/columns", f"Nicht verschoben: {exc}", "error")
        return redirect(request, "/columns")

    @app.post("/groups/{action}")
    async def group_action(request: Request, action: str):
        form = {k: str(v) for k, v in (await request.form()).items()}
        store, group = data(request), form.get("group", "")
        try:
            if action == "new":
                store.add_group(form.get("name", ""))
            elif action == "rename":
                store.rename_group(group, form.get("name", ""))
            elif action == "move":
                store.move_group(group, int(form.get("offset", "0")))
            elif action == "delete":
                store.delete_group(group)
            else:
                raise HTTPException(404)
        except ValueError as exc:
            return redirect(request, "/columns", f"Nicht gespeichert: {exc}", "error")
        return redirect(request, "/columns")

    # -- settings, import, export ------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(request, "settings.html", settings=data(request).settings(), entry_count=len(data(request).entries()))

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        try:
            target, _ = fx.parse_number_input(str(form.get("target", "")))
            if not target or target <= 0:
                raise fx.FormulaError("Ziel muss größer als 0 sein")
        except fx.FormulaError as exc:
            return redirect(request, "/settings", f"Nicht gespeichert: {exc}", "error")
        data(request).set_settings({"target": str(target),
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
            result = import_workbook(await upload.read(), data(request).columns())
            data(request).replace_all(result.new_columns, result.entries, result.income)
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
    def export_excel(request: Request):
        columns, entries, rows = sheet(request)
        content = export_workbook(columns, entries, rows, data(request).income())
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": _attachment("NetWorth", "xlsx")})

    # -- account and user administration -----------------------------------

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        return page(request, "account.html")

    @app.post("/account/password")
    async def account_password(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        user = request.state.user
        if users.authenticate(user.username, form.get("current", "")) is None:
            return redirect(request, "/account", "Das aktuelle Passwort ist falsch", "error")
        try:
            if form.get("password", "") != form.get("password2", ""):
                raise UserError("Die Passwörter stimmen nicht überein")
            users.set_password(user.id, form.get("password", ""))
        except UserError as exc:
            return redirect(request, "/account", f"Nicht geändert: {exc}", "error")
        return redirect(request, "/account", "Passwort geändert")

    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request):
        accounts = [(u, users.store_path(u.id).stat().st_size if users.store_path(u.id).exists() else 0)
                    for u in users.all()]
        return page(request, "admin_users.html", accounts=accounts, registration=users.registration_open())

    def other_user(request: Request, user_id: int) -> User:
        user = users.get(user_id)
        if user is None:
            raise HTTPException(404, "Benutzer nicht gefunden")
        return user

    @app.post("/admin/users")
    async def admin_user_create(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            user = users.create(form.get("username", ""), form.get("password", ""), is_admin=bool(form.get("is_admin")))
        except UserError as exc:
            return redirect(request, "/admin/users", f"Nicht angelegt: {exc}", "error")
        return redirect(request, "/admin/users", f"Benutzer „{user.username}“ angelegt")

    @app.post("/admin/registration")
    async def admin_registration(request: Request):
        form = await request.form()
        users.set_registration_open(bool(form.get("open")))
        return redirect(request, "/admin/users", "Registrierung " + ("geöffnet" if form.get("open") else "geschlossen"))

    @app.post("/admin/users/{user_id}/password")
    async def admin_user_password(request: Request, user_id: int):
        user = other_user(request, user_id)
        form = await request.form()
        try:
            users.set_password(user.id, str(form.get("password", "")))
        except UserError as exc:
            return redirect(request, "/admin/users", f"Nicht geändert: {exc}", "error")
        return redirect(request, "/admin/users", f"Passwort von „{user.username}“ geändert")

    @app.post("/admin/users/{user_id}/admin")
    async def admin_user_role(request: Request, user_id: int):
        user = other_user(request, user_id)
        make_admin = bool((await request.form()).get("is_admin"))
        try:
            users.set_admin(user.id, make_admin)
        except UserError as exc:
            return redirect(request, "/admin/users", str(exc), "error")
        return redirect(request, "/admin/users", f"„{user.username}“ ist jetzt "
                        + ("Administrator" if make_admin else "normaler Benutzer"))

    @app.post("/admin/users/{user_id}/delete")
    def admin_user_delete(request: Request, user_id: int):
        user = other_user(request, user_id)
        if user.id == request.state.user.id:
            return redirect(request, "/admin/users", "Das eigene Konto kann nicht gelöscht werden", "error")
        try:
            users.delete(user.id)
        except UserError as exc:
            return redirect(request, "/admin/users", str(exc), "error")
        with cache_lock:
            for key in [k for k in cache if k.startswith(f"{user.id}:")]:
                del cache[key]
        return redirect(request, "/admin/users", f"Benutzer „{user.username}“ und alle Daten gelöscht")

    return app


class UserSwitched(Exception):
    def __init__(self, username: str):
        super().__init__(username)
        self.username = username


def _path(request: Request) -> str:
    return request.url.path.removeprefix(request.scope.get("root_path", ""))


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


def _nav(user: User | None) -> list[list[tuple[str, str]]]:
    return NAV[:-1] + [NAV[-1] + ADMIN_NAV] if user is not None and user.is_admin else NAV


def _nav_html(root: str, active: str, user: User, logout: bool) -> str:
    """Menu of the top bar (``dashboard.toolbar_html``); ``active``: the current path without ``root``."""
    def link(href: str, label: str, css: str = "") -> str:
        classes = " ".join(filter(None, [css, "active" if href == active else ""]))
        attributes = (f' class="{classes}"' if classes else "") + (' aria-current="page"' if href == active else "")
        return f'<a href="{html.escape(root + href)}"{attributes}>{html.escape(label)}</a>'

    links = '<span class="nav-sep"></span>'.join("".join(link(*item) for item in group) for group in _nav(user))
    account = ""
    if logout:
        account = (f'<div class="account">{link("/account", user.username)}'
                   f'<form method="post" action="{html.escape(root)}/logout">'
                   '<button type="submit">Abmelden</button></form></div>')
    return f'<nav aria-label="Hauptmenü">{links}</nav>{link(*NEW_ENTRY, css="cta")}{account}'
