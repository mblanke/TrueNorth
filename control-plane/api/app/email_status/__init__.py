"""Whether the API can send email, and a test message for the platform administrator (#306).

The relay is set at install time (``tn_smtp_*``; install/README.md "Email (SMTP)") and read
by ``app.notifications.smtp.smtp_settings``. This section only reports on it and proves it;
it changes nothing. No table of its own: a test send is an ``audit_logs`` row.
"""
