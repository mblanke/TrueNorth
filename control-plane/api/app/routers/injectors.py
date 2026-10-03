"""TrueNorth Range — Inject catalogue router.

Read-only introspection of the scenario-engine injector registry, so authoring
UIs can offer the real action list instead of hard-coding their own. Before
this endpoint existed the Scenario Builder and the ops-center inject dialog
each carried an independent, mutually inconsistent subset of the registry.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from .. import engine_bridge
from ..auth import CurrentUser, get_current_user

logger = logging.getLogger("truenorth.api.injectors")

router = APIRouter(prefix="/injectors", tags=["injectors"])


@router.get("")
def list_injectors(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
    """The registered injectors with their authoring metadata.

    ``[{name, description, required_params, mitre_techniques}]``. Injectors
    that declare no required params return an empty list — the UI renders a
    free-form params editor for those.
    """
    return engine_bridge.injector_catalogue()
