from fastapi import APIRouter

from app.api.admin import router as admin_router
from app.api.agent_access import router as agent_access_router
from app.api.assistant import router as assistant_router
from app.api.auth import admin_auth_router, router as auth_router
from app.api.business_doc_updates import router as business_doc_updates_router
from app.api.code_reviews import router as code_reviews_router
from app.api.deploy import router as deploy_router
from app.api.health import router as health_router
from app.api.my_tasks import router as my_tasks_router
from app.api.requirement_pull_requests import router as requirement_pull_requests_router
from app.api.requirement_reviews import router as requirement_reviews_router
from app.api.security_scan import router as security_scan_router
from app.api.workspace import router as workspace_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(admin_auth_router)
api_router.include_router(admin_router)
api_router.include_router(agent_access_router)
api_router.include_router(assistant_router)
api_router.include_router(requirement_reviews_router)
api_router.include_router(requirement_pull_requests_router)
api_router.include_router(my_tasks_router)
api_router.include_router(business_doc_updates_router)
api_router.include_router(code_reviews_router)
api_router.include_router(deploy_router)
api_router.include_router(security_scan_router)
api_router.include_router(workspace_router)
