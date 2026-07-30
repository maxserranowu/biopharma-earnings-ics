.PHONY: test smoke build dry validate
test:     ; python -m pytest tests/test_pipeline.py -q
smoke:    ; python -m tests.smoke
dry:      ; python -m src.build --dry-run --no-enrich -v
build:    ; python -m src.build
validate: ; python -m tests.validate docs/*.ics
