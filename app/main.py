import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.db import init_pool
from app.deps import session_user_id
from app.routers.auth import router as auth_router
from app.routers.customer import router as customer_router
from app.routers.cashier import router as cashier_router
from app.routers.staff import router as staff_router
from app.routers.admin import router as admin_router
from app.routers.admin_ops import router as admin_ops_router
from app.routers.activity_ui import router as activity_ui_router

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_pool()
    from app.automation import start_scheduler, stop_scheduler
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="PharmaLink", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", secrets.token_hex(32)),
    same_site="lax",
    https_only=False,
)

app.include_router(auth_router)
app.include_router(customer_router)
app.include_router(cashier_router)
app.include_router(staff_router)
app.include_router(admin_router)
app.include_router(admin_ops_router)
app.include_router(activity_ui_router)


@app.get("/sw.js")
def service_worker():
    return FileResponse(ROOT / "assets" / "sw.js", media_type="text/javascript")


@app.get("/")
@app.get("/index.html")
@app.get("/index.php")
@app.get("/login.php")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/reset-password.html")
def reset_password_page():
    return FileResponse(ROOT / "reset-password.html")


@app.get("/logout.php")
@app.post("/logout.php")
def legacy_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


@app.get("/customer/customer.php")
@app.get("/customer/customer.html")
def customer_portal(request: Request):
    role = str(request.session.get("user_role") or "").lower()
    if session_user_id(request) is not None and role and role != "customer":
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(ROOT / "customer" / "customer.html")


@app.get("/cashier/cashier.php")
@app.get("/cashier/cashier.html")
def cashier_portal(request: Request):
    role = str(request.session.get("user_role") or "").lower()
    if session_user_id(request) is None or role not in ("cashier/pharmacist", "admin"):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(ROOT / "cashier" / "cashier.html")


@app.get("/admin/dashboard.html")
@app.get("/admin/dashboard.php")
@app.get("/admin/upload_prescription.html")
def admin_legacy_pages():
    return RedirectResponse(url="/admin/admin.html", status_code=302)


@app.get("/admin/admin.php")
@app.get("/admin/admin.html")
def admin_portal(request: Request):
    role = str(request.session.get("user_role") or "").lower()
    if session_user_id(request) is None or role != "admin":
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(ROOT / "admin" / "admin.html")


app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
app.mount("/shared", StaticFiles(directory=ROOT / "shared"), name="shared")
app.mount("/customer/css", StaticFiles(directory=ROOT / "customer" / "css"), name="customer_css")
app.mount("/customer/js", StaticFiles(directory=ROOT / "customer" / "js"), name="customer_js")
app.mount("/cashier/css", StaticFiles(directory=ROOT / "cashier" / "css"), name="cashier_css")
app.mount("/cashier/js", StaticFiles(directory=ROOT / "cashier" / "js"), name="cashier_js")
app.mount("/admin/css", StaticFiles(directory=ROOT / "admin" / "css"), name="admin_css")
app.mount("/admin/js", StaticFiles(directory=ROOT / "admin" / "js"), name="admin_js")
app.mount("/uploads", StaticFiles(directory=ROOT / "uploads"), name="uploads")
