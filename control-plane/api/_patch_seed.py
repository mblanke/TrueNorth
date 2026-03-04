import pathlib

MAIN = pathlib.Path(r"D:\Projects\Dev\TrueNorth Range\Core_Docs\control-plane\api\app\main.py")
content = MAIN.read_text(encoding="utf-8")

# Find the end of the existing _seed_dev_data function and add nation/coalition seeding
old_seed_end = '''            logger.info("Seeded default tenant and admin user")
    except Exception as e:
        db.rollback()
        logger.warning("Seed failed (may already exist): %s", e)
    finally:
        db.close()'''

new_seed_end = '''            logger.info("Seeded default tenant and admin user")
        # Seed nations, coalitions, and auth zones
        from .seed import seed_nations_and_coalitions, seed_auth_zones
        seed_nations_and_coalitions(db)
        seed_auth_zones(db)
    except Exception as e:
        db.rollback()
        logger.warning("Seed failed (may already exist): %s", e)
    finally:
        db.close()'''

content = content.replace(old_seed_end, new_seed_end)

MAIN.write_text(content, encoding="utf-8", newline="\n")
print("main.py patched with nation/coalition/auth-zone seeding")