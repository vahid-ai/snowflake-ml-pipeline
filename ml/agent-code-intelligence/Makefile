.PHONY: test doctor profiles zip

test:
	python3 tests/test_plugin.py

doctor:
	python3 scripts/tool_doctor.py

profiles:
	python3 scripts/render_mcp_config.py minimal

zip:
	cd .. && zip -qr agent-code-intelligence-1.1.0.zip agent-code-intelligence-1.1.0
