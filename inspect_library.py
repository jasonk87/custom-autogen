import os
import sys

path = r"C:\Users\Jason\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\LocalCache\local-packages\Python311\site-packages\autogen_ext\models\openai\_openai_client.py"

try:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    start = 715
    end = 730
    print(f"--- CONTENT OF {path} ({start}-{end}) ---")
    for i in range(start, end):
        if i < len(lines):
            sys.stdout.write(f"{i}: {lines[i]}")
            sys.stdout.flush()

except Exception as e:
    print(f"Error reading file: {e}")
