.PHONY: setup run demo test
setup:            ## create .env from the example (edit it and add your key)
	@test -f .env || cp .env.example .env; echo "Edit .env and set CRUSTDATA_API_KEY"
run:              ## find new stealth founders and post the digest to Slack
	python3 radar.py
demo:             ## same as run, but never posts to Slack
	python3 radar.py --dry-run --all
test:
	python3 -m unittest discover -s tests -v
