"""
Convierte .secrets/service_account.json + una contraseña elegida en
.streamlit/secrets.toml (el formato que usa Streamlit, tanto local como
en Streamlit Community Cloud).

Uso:
    python generar_secrets.py
"""
import json
import os

JSON_KEY_PATH = ".secrets/service_account.json"
OUT_DIR = ".streamlit"
OUT_PATH = os.path.join(OUT_DIR, "secrets.toml")

with open(JSON_KEY_PATH, encoding="utf-8") as f:
    sa = json.load(f)

password = input("Elegi la contraseña de acceso al dashboard: ").strip()
if not password:
    raise SystemExit("La contraseña no puede estar vacia.")

os.makedirs(OUT_DIR, exist_ok=True)

lines = ['APP_PASSWORD = "%s"' % password.replace('"', '\\"'), "", "[gcp_service_account]"]
for k, v in sa.items():
    v_escaped = str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    lines.append(f'{k} = "{v_escaped}"')

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

print(f"Listo. Generado {OUT_PATH}")
print("Este archivo NO se sube a git (esta en .gitignore).")
