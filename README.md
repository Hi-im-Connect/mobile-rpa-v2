# Mobile RPA v2

Copy of Mobile RPA (v1 stays live at /mobile/) where the FastAutomate v2 app runs the agent on the
phone and this dashboard plans, assigns and reports. Live at https://digimate.fastautomate.com/mobile2/.

- Tests: `.venv/bin/python -m pytest -q`
- Deploy: `./deploy/deploy.sh` (CT 124, port 8091, data /var/lib/mobile-rpa-v2)
- Design: docs/superpowers/specs/2026-09-25-on-phone-agent-design.md
