"""The local web interface for the five-stage pipeline.

Standard library only - no Flask, no Node, no build step. ``python main.py ui`` starts
a server on localhost, serves ``static/`` and exposes the pipeline over JSON.

    server.py   the HTTP server, the JSON API and the background job runner
    static/     the page itself: index.html, style.css, app.js
    __main__.py lets ``python -m src.ui`` start it too
"""
