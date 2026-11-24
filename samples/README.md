Put Power BI files here (.pbix, .pbit) to run them from the CLI or the sample test:

    python -m pbi_insights samples\YourReport.pbix --out outputs --data
    python -m pytest -q tests\test_pipeline.py

Everything in this folder except this note is ignored by git, so client files are never committed.
