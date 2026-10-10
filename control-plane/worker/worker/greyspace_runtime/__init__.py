"""The Greyspace stack generator, as the worker ships it (ADR 0007).

SOURCE OF TRUTH: control-plane/api/app/greyspace/ (config.py, manifest.py, fixture.py,
npc.py, npc_agent.py, gs_cli.py). The worker and API build separate images and neither may
import the other (MOSA: api_imports_worker), so the worker carries byte-identical copies
here, as it does for secretbox.py. tests/contracts/test_greyspace_runtime_copy.py fails
if they differ; to update: ``cp control-plane/api/app/greyspace/{config,manifest,fixture,
npc,npc_agent,gs_cli}.py control-plane/worker/worker/greyspace_runtime/``.
"""
