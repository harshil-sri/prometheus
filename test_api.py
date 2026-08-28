import requests

try:
    print("Init...")
    r = requests.post("http://127.0.0.1:8000/api/init")
    print(r.json())

    print("Demo run...")
    r2 = requests.post("http://127.0.0.1:8000/api/demo/run")
    print(r2.json())
except Exception as e:
    print(f"Error: {e}")
