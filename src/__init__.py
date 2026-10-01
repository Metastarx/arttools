"""Art-asset pipeline: run ``python main.py <command>`` (see ``python main.py --help``).

This file is what makes ``src`` a real package instead of a folder that happened to be
importable. It is deliberately empty of imports: the stage modules pull in numpy,
Pillow and scipy, and something as small as ``python -c "import src"`` should not pay
for that.

Layout:

    paths.py    where the project keeps its files - the single place that decides
    config.py   API key, base URL, model and request settings
    harness.py  reads hareness/ (common.yaml, characters/, style/)
    api.py      the HTTP call to the relay, one function per calling convention
    generator.py  prompt assembly + draw one spec
    chroma.py   flat-backdrop keying, padding, cropping  (stages 2 and 5)
    postprocess.py  crop, resize, align, sprite sheets
    video.py    Seedance calls, prompts, frame extraction  (stage 4)
    stages.py   the five stages and the code that runs them in order
    animation.py  the older still-image animation path
    ui/         the local web interface
    cli.py      argument parsing, the ``main.py`` commands
"""
