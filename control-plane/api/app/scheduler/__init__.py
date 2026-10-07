"""Scheduler: bookings, their lifecycle and the calendar (docs/adr/0004-scheduler-module.md).

Other sections use :mod:`app.scheduler.service`; nothing outside this package reads
``scheduled_events`` directly. Deliberately empty so ``app.models`` can import
:mod:`app.scheduler.models` without pulling in the router.
"""
