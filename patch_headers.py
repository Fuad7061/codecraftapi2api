import re

with open("app/pool.py", "r") as f:
    content = f.read()

new_headers = """        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "referer": url,
            "origin": base,
            "x-xsrf-token": _xsrf(s) or "",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            **_headers_extra(cfg),
        }"""

content = re.sub(r'        headers = \{.*?\*\*_headers_extra\(cfg\),\n        \}', new_headers, content, flags=re.DOTALL)

with open("app/pool.py", "w") as f:
    f.write(content)
