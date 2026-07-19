"""TrueNorth Range - API Router package."""

from .ad_sync import router as ad_sync_router
from .adaptive_learning import router as adaptive_learning_router
from .admin import router as admin_router
from .ai_config import router as ai_config_router
from .auth_zones import router as auth_zones_router
from .competency import certs_router as certifications_router
from .competency import router as competency_router
from .courses import lp_router as learning_paths_router
from .courses import router as courses_router
from .courses import transcript_router
from .curriculum import router as curriculum_router
from .detection_rules import router as detection_rules_router
from .directory import router as directory_router
from .golden_images import router as golden_images_router
from .exercise_forge import router as exercise_forge_router
from .exercises import router as exercises_router
from .exercises_collective import router as collective_exercises_router
from .hypervisors import router as hypervisors_router
from .integrations import lti_router
from .integrations import router as integrations_router
from .kit import router as kit_router
from .network_devices import router as network_devices_router
from .ops_center import router as ops_center_router
from .proxmox import router as proxmox_router
from .qsp import router as qsp_router
from .quizzes import router as quizzes_router
from .ranges import router as ranges_router
from .scenarios import router as scenarios_router
from .scheduling import router as scheduling_router
from .storage import router as storage_router
from .templates import router as templates_router
from .threat_intel import router as threat_intel_router

__all__ = [
    "ranges_router",
    "exercises_router",
    "collective_exercises_router",
    "exercise_forge_router",
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
    "golden_images_router",
    "ad_sync_router",
    "auth_zones_router",
    "storage_router",
    "network_devices_router",
    "kit_router",
    "threat_intel_router",
    "detection_rules_router",
    "exercise_forge_router",
    "adaptive_learning_router",
    "ops_center_router",
    "curriculum_router",
    "qsp_router",
    "quizzes_router",
]
