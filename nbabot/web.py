from contextlib import asynccontextmanager
import base64
import binascii
import fcntl
import os
import tempfile
import uuid
from pathlib import Path
from secrets import compare_digest
from urllib.parse import unquote

from fastapi import BackgroundTasks,FastAPI, HTTPException,Request
from fastapi.responses import FileResponse, Response,JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import Store, load_config
from .report import overview


def create_app(config=None):
    config=config or load_config()
    user=os.environ.get("NBABOT_DASHBOARD_USER", "")
    password=os.environ.get("NBABOT_DASHBOARD_PASSWORD", "")
    if bool(user)!=bool(password):
        raise ValueError("Set both NBABOT_DASHBOARD_USER and NBABOT_DASHBOARD_PASSWORD, or neither")
    spreadsheet=config.get("data_source_mode")=="spreadsheet"
    if spreadsheet:
        from .first_boot import require_private_auth
        require_private_auth()
    @asynccontextmanager
    async def lifespan(app):
        from .maintenance import recover
        store=Store(config)
        try:
            if spreadsheet:
                from .first_boot import read_status,write_status
                if read_status(store.root).get("status") in ("validating_master","refreshing"):
                    write_status(store.root,"error",error_type="InterruptedProcessing",
                        note="The previous processing was interrupted. Any last validated master is retained; upload again to regenerate outputs.")
            recover(store)
        finally:
            store.close()
        yield
    app=FastAPI(title="Knicks Projection Desk",version="0.1.0",lifespan=lifespan)
    static=Path(__file__).parent/"static"
    app.mount("/static",StaticFiles(directory=static),name="static")

    @app.get("/")
    def index():
        return FileResponse(static/"index.html")

    @app.get("/api/overview")
    def get_overview():
        store=Store(config)
        try:
            return overview(store)
        finally:
            store.close()

    @app.get("/health")
    def health():
        store=Store(config)
        try:
            return {"status":"ok","database":bool(store.one("SELECT 1")),"meaning":"Application is reachable; source readiness is in /api/overview"}
        finally:
            store.close()

    def master_path():
        from .master_source import master_status
        store=Store(config)
        try:
            source=master_status(store)
            relative=Path(source.get("relative_path", ""))
            path=store.root/relative
            if (source.get("status")!="ready" or relative.is_absolute() or ".." in relative.parts
                or not path.resolve().is_relative_to((store.root/"originals").resolve()) or not path.is_file()):
                raise HTTPException(404,"No validated master workbook has been uploaded")
            from .core import digest
            if digest(path.read_bytes())!=source["file_hash"]:
                raise HTTPException(409,"The accepted master has changed; upload and validate it again")
            return path,source
        finally:
            store.close()

    @app.get("/api/master/download")
    def master_download():
        if not spreadsheet:
            raise HTTPException(404)
        path,source=master_path()
        return FileResponse(path,filename=source["original_name"],
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.post("/api/master")
    async def upload_master(request:Request,background:BackgroundTasks):
        if not spreadsheet:
            raise HTTPException(404)
        if request.headers.get("X-NBAPPBOT-Upload")!="master":
            raise HTTPException(403,"Use the authenticated master upload control")
        maximum=32*1024*1024
        try:
            length=int(request.headers.get("content-length",0))
        except ValueError:
            raise HTTPException(400,"Invalid content length")
        if length<0 or length>maximum:
            raise HTTPException(413,"The workbook must be at most 32 MiB")
        root=Path(config["data_dir"])
        root.mkdir(parents=True,exist_ok=True)
        upload_lock=(root/".upload.lock").open("a")
        try:
            fcntl.flock(upload_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            upload_lock.close()
            raise HTTPException(409,"Another master upload is being processed")
        queued=False;temporary=None
        try:
            incoming=root/"incoming";incoming.mkdir(exist_ok=True)
            fd,name=tempfile.mkstemp(prefix="master-",suffix=".xlsx",dir=incoming)
            temporary=Path(name);total=0
            with os.fdopen(fd,"wb") as output:
                async for chunk in request.stream():
                    total+=len(chunk)
                    if total>maximum:
                        raise HTTPException(413,"The workbook must be at most 32 MiB")
                    output.write(chunk)
            if not total:
                raise HTTPException(400,"Choose a nonempty XLSX workbook")
            original_name=Path(unquote(request.headers.get("X-File-Name","Master.xlsx"))).name
            if not original_name.lower().endswith(".xlsx"):
                raise HTTPException(415,"Choose an XLSX master workbook")
            job=uuid.uuid4().hex
            from .first_boot import write_status
            write_status(root,"validating_master",job_id=job,original_name=original_name)

            def process():
                from .first_boot import write_status
                from .master_source import import_master,refresh_master
                store=None
                try:
                    store=Store(config)
                    with (root/".refresh.lock").open("a") as maintenance_lock:
                        fcntl.flock(maintenance_lock,fcntl.LOCK_EX)
                        imported=import_master(store,temporary,original_name=original_name,lock_held=True)
                        refreshed=refresh_master(store,lock_held=True)
                    write_status(root,"ready",job_id=job,original_name=original_name,
                        import_status=imported.get("status"),refresh_status=refreshed.get("status"))
                except Exception as exc:
                    if store:
                        store.db.rollback()
                    write_status(root,"error",job_id=job,error_type=type(exc).__name__,
                        error=str(exc)[:700],note="The last validated master remains active if one was previously accepted.")
                finally:
                    if store:store.close()
                    temporary.unlink(missing_ok=True)
                    fcntl.flock(upload_lock,fcntl.LOCK_UN);upload_lock.close()
            background.add_task(process)
            queued=True
            return JSONResponse({"status":"validating_master","job_id":job,
                "note":"Processing the master workbook privately; inspect /api/overview for the result"},status_code=202,background=background)
        finally:
            if not queued:
                if temporary:temporary.unlink(missing_ok=True)
                fcntl.flock(upload_lock,fcntl.LOCK_UN);upload_lock.close()

    @app.get("/api/export/{name}")
    def download(name:str):
        if spreadsheet and name in ("Master_Data.xlsx","Knicks_Historical_Organized.xlsx"):
            return master_download()
        root=Path(config["data_dir"])/"packages/latest"
        if Path(name).name!=name or name not in ("Master_Data.xlsx","Knicks_Historical_Organized.xlsx","organized_validation.json","organized_missing.csv","organized_changelog.md","NBA_Player_Games.csv","Games.csv","Players.csv","Snapshots.csv","Source_Registry.csv","manifest.json"):
            raise HTTPException(404)
        path=root/name
        if not path.is_file():
            raise HTTPException(404,"No verified export has been published")
        return FileResponse(path,filename=name)

    @app.middleware("http")
    async def safe_headers(request,call_next):
        authorized=not user or request.url.path=="/health"
        if not authorized:
            try:
                scheme,encoded=request.headers.get("authorization", "").split(" ",1)
                supplied_user,separator,supplied_password=base64.b64decode(encoded,validate=True).decode("utf-8").partition(":")
                authorized=(scheme.lower()=="basic" and bool(separator)
                    and compare_digest(supplied_user.encode(),user.encode())
                    and compare_digest(supplied_password.encode(),password.encode()))
            except (ValueError,UnicodeError,binascii.Error):
                authorized=False
        response=await call_next(request) if authorized else Response("Authentication required",status_code=401,
            headers={"WWW-Authenticate":'Basic realm="Knicks Projection Desk", charset="UTF-8"'})
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Content-Security-Policy"]="default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        response.headers["Cache-Control"]="no-store"
        return response
    return app


app=create_app()
