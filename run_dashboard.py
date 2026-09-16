import os
import subprocess
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
subprocess.run(
    [sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", "8510"],
    check=True,
)
