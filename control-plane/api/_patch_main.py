import pathlib

MAIN = pathlib.Path(r"D:\Projects\Dev\TrueNorth Range\Core_Docs\control-plane\api\app\main.py")
content = MAIN.read_text(encoding="utf-8")

# 1. Add new router imports to the import block
old_import_end = "    lti_router,\n)"
new_import_end = """    lti_router,
    hypervisors_router,
    ai_config_router,
    directory_router,
    ad_sync_router,
    auth_zones_router,
)"""
content = content.replace(old_import_end, new_import_end)

# 2. Add include_router calls after lti_router
old_include = "app.include_router(lti_router)"
new_include = """app.include_router(lti_router)
# Infrastructure & Directory routers
app.include_router(hypervisors_router)
app.include_router(ai_config_router)
app.include_router(directory_router)
app.include_router(ad_sync_router)
app.include_router(auth_zones_router)"""
content = content.replace(old_include, new_include)

MAIN.write_text(content, encoding="utf-8", newline="\n")
print("main.py patched with 5 new router registrations")