"""
Script de prueba: verifica que la Service Account puede leer la hoja
y muestra los encabezados (fila 1) de la pestaña Actual.

Uso:
    python test_conexion.py
"""
import gspread
from google.oauth2.service_account import Credentials

JSON_KEY_PATH = ".secrets/service_account.json"
SPREADSHEET_ID = "1FwPVTMoDBo-Mc5_XAj5ZUiB6pv7AzM6gADDf8s_MLAo"
TAB_NAME = "Actual"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

creds = Credentials.from_service_account_file(JSON_KEY_PATH, scopes=SCOPES)
client = gspread.authorize(creds)

sheet = client.open_by_key(SPREADSHEET_ID)
ws = sheet.worksheet(TAB_NAME)

headers = ws.row_values(1)
print(f"Conexion OK. {len(headers)} columnas encontradas en '{TAB_NAME}':\n")
for i, h in enumerate(headers, start=1):
    print(f"  {i:2d}. {h}")

print(f"\nFilas totales (aprox): {ws.row_count}")
row2 = ws.row_values(2)
print("\nPrimera fila de datos (fila 2):")
for h, v in zip(headers, row2):
    print(f"  {h}: {v}")
