"""TrueNorth Range - API Router package."""
from .ranges import router as ranges_router
from .exercises import router as exercises_router
from .templates import router as templates_router
from .scenarios import router as scenarios_router
from .admin import router as admin_router
from .proxmox import router as proxmox_router
from .scheduling import router as scheduling_router
from .courses import router as courses_router
from .courses import lp_router as learning_paths_router
from .courses import transcript_router
from .competency import router as competency_router
from .competency import certs_router as certifications_router
from .integrations import router as integrations_router
from .integrations import lti_router
from .hypervisors import router as hypervisors_router
from .ai_config import router as ai_config_router
from .directory import router as directory_router
from .ad_sync import router as ad_sync_router
from .auth_zones import router as auth_zones_router
from .storage import router as storage_router
from .network_devices import router as network_devices_router
from .kit import router as kit_router

__all__ = [
    "ranges_router",
    "exercises_router",
    "templates_router",
    "scenarios_router",
    "admin_router",
    "proxmox_router",
    "scheduling_router",
    "courses_router",
    "learning_paths_router",
    "transcript_router",
    "competency_router",
    "certifications_router",
    "integrations_router",
    "lti_router",
    "hypervisors_router",
    "ai_config_router",
    "directory_router",
    "ad_sync_router",
    "auth_zones_router",
    "storage_router",
    "network_devices_router",
    "kit_router",
]