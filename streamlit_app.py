"""Entry point for Streamlit Community Cloud.

The UI lives in src/hgc/ui/app.py and imports `hgc.ui.api_client`, so we add `src` to the
import path, then run the app module. Set HGC_API_URL in the deployed app's Secrets to
point at your running API (Streamlit exposes secrets as environment variables).
"""

import os
import runpy
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
runpy.run_module("hgc.ui.app", run_name="__main__")
