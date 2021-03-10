"""Install Dallinger as a command line utility."""

from setup_utils import update_pins
from setuptools import setup

setup_args = dict(
    name="dallinger",
    packages=["dallinger", "dallinger_scripts"],
    version="7.0.0",
    description="Laboratory automation for the behavioral and social sciences",
    url="http://github.com/Dallinger/Dallinger",
    maintainer="Jordan Suchow",
    maintainer_email="suchow@berkeley.edu",
    license="MIT",
    keywords=["science", "cultural evolution", "experiments", "psychology"],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "Topic :: Scientific/Engineering",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Framework :: Pytest",
    ],
    include_package_data=True,
    zip_safe=False,
    entry_points={
        "console_scripts": [
            "dallinger = dallinger.command_line:dallinger",
            "dallinger-housekeeper = dallinger.command_line:dallinger_housekeeper",
            "dallinger_heroku_web = dallinger_scripts.web:main",
            "dallinger_heroku_worker = dallinger_scripts.worker:main",
            "dallinger_heroku_clock = dallinger_scripts.clock:main",
        ],
        "dallinger.experiments": [],
        "pytest11": ["pytest_dallinger = dallinger.pytest_dallinger"],
    },
    extras_require={
        "jupyter": [
            "jupyter",
            "ipywidgets",
        ],
        "data": [
            "pandas",
            "tablib[all]",
        ],
        "dev": [
            "alabaster",
            "bumpversion",
            "coverage",
            "coverage_pth",
            "codecov",
            "flake8",
            "mock",
            "pycodestyle",
            "pypandoc",
            "pytest",
            "recommonmark",
            "sphinxcontrib-spelling",
            "Sphinx",
            "tox",
            "sphinx-js",
            "sphinx_rtd_theme",
        ],
        "docker": ["docker", "docker-compose"],
        ':python_version <= "3.7"': ["importlib_metadata"],
    },
)

update_pins(setup_args)

setup(**setup_args)
