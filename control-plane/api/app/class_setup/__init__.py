"""An instructor setting up their own class without an admin (#200).

``scope`` decides which courses a caller teaches and which exercises they run; the
course enrolment routes (routers/courses.py) and the team routes (routers/admin.py) ask
it. ``router`` serves GET /class-setup/mine so the web app can show the controls.
"""
