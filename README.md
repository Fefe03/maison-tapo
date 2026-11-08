# MAISON · Smart Home

Ein kleines Hotel-Kiosk-Dashboard: dunkles Oliv, warme goldene Lichtpunkte, interaktiver SVG-Grundriss und responsive Gerätekacheln. Single-File-Frontend, Python-Cloud-Backend, keine lokale Geräte-IP.

## In 3 Schritten starten

### 1. Dateien auf GitHub bereitstellen und die Demo testen

`index.html` im Browser öffnen: Die markierte Demo funktioniert ohne Konto und Backend. Pins, Gerätekacheln und „Alles ausschalten“ sind direkt bedienbar. Für einen lokalen Webserver im Projektordner:

```powershell
python -m http.server 8080 --bind 127.0.0.1
```

Dann http://127.0.0.1:8080 öffnen. Die Dateien `main.py`, `requirements.txt`, `index.html` und optional `render.yaml` in ein GitHub-Repository laden. **Keine Zugangsdaten, `.env` oder `.venv` hochladen.**

### 2. Backend auf Render starten

In Render **New → Web Service**, GitHub-Repository auswählen, Runtime **Python 3**, Instanz **Free** einstellen:

| Einstellung | Wert |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health Check | `/health` |
| `PYTHON_VERSION` | `3.12.10` |
| `TAPO_USERNAME` | E-Mail des TP-Link-Kontos |
| `TAPO_PASSWORD` | Passwort des TP-Link-Kontos |
| `DASHBOARD_TOKEN` | Separater zufälliger Zugangsschlüssel, mindestens 24 Zeichen |
| `CORS_ORIGINS` | `*` (Standard); optional z. B. `https://deinname.github.io` |

Zugangsschlüssel lokal erzeugen: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. In Render unter **Environment** speichern; nicht im HTML. Alternativ das mitgelieferte `render.yaml` über **New → Blueprint** verwenden. Es erzeugt den Dashboard-Token automatisch; den Wert anschließend in Render nachsehen.

Nach dem Deploy die `https://…onrender.com`-Adresse kopieren. `/health` bestätigt nur den Webserver, nicht die Anmeldung bei TP-Link. Der erste authentifizierte Geräteabruf prüft die Cloud-Verbindung. Render Free schläft nach 15 Minuten ohne Anfragen ein; der nächste Aufruf benötigt ungefähr eine Minute. [Render-Anleitung](https://render.com/docs/deploy-fastapi), [Free-Limits](https://render.com/docs/free).

### 3. Frontend verbinden – lokal oder mit GitHub Pages

In `index.html` die Konstante `BACKEND_URL` auf die Render-URL setzen. Alternativ oben rechts in den Einstellungen die URL eingeben. Danach dort den **Dashboard-Zugangsschlüssel** eintragen. Dieser bleibt nur im Speicher des Tabs; nach Neuladen erneut eingeben. Die Backend-URL wird lokal gespeichert.

Lokal kannst du dieselbe HTML-Datei oder den Webserver aus Schritt 1 nutzen. Auf GitHub: **Settings → Pages → Source: GitHub Actions**. Der mitgelieferte Workflow `.github/workflows/pages.yml` veröffentlicht ausschließlich `index.html` bei Änderungen auf `main`. Nach erstmaliger Aktivierung unter **Actions → Dashboard veröffentlichen → Run workflow** starten. Anschließend die angezeigte Pages-Adresse öffnen und verbinden. GitHub Pages liefert nur das Frontend; Python läuft auf Render. [GitHub-Pages-Anleitung](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

## Raumzuordnung

Die Namen in `ROOM_ALIASES` müssen den eindeutigen Alias-Namen in der Tapo-App entsprechen:

```javascript
const ROOM_ALIASES = {
  "Meine Stehlampe": "living",
  "Meine Nachttischlampe": "bedroom",
  "Licht Küche": "kitchen"
};
```

Alternativ werden Namen mit „Wohnzimmer“, „Schlafzimmer“, „Küche“ oder „Flur“ automatisch zugeordnet. Unbekannte Namen bleiben als Kachel sichtbar. Der schematische Plan bietet sechs Pins im Wohnzimmer und je vier in den anderen Räumen; weitere Geräte erscheinen nur als Kachel. Die Pin-Koordinaten stehen in `POSITIONS`. Alle gefundenen Steckdosen werden von „Alles ausschalten“ erfasst, unabhängig von ihrer Raumzuordnung.

## Backend lokal betreiben

Python 3.12 verwenden. In PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:TAPO_USERNAME = Read-Host 'TP-Link E-Mail'
$env:TAPO_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host 'TP-Link Passwort' -AsSecureString)).Password
$env:DASHBOARD_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Dashboard-Zugangsschlüssel' -AsSecureString)).Password
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

http://127.0.0.1:8000 öffnen, in den Dashboard-Einstellungen dieselbe URL und den Zugangsschlüssel eingeben. `.env.example` dokumentiert die Variablen; `.env` wird bewusst nicht automatisch geladen, damit nur drei Laufzeitabhängigkeiten nötig sind.

## API und Verhalten

Alle `/api/…`-Aufrufe benötigen `Authorization: Bearer <DASHBOARD_TOKEN>`.

| Endpunkt | Ergebnis |
|---|---|
| `GET /api/devices` | Liste mit `id`, `alias`, `model`, `on`, `online`, `controllable`, `error` |
| `POST /api/device/{alias}/toggle` | Schaltet einmal und liest den neuen Zustand zurück; Alias URL-kodieren |
| `POST /api/devices/off` | Setzt alle gefundenen Steckdosen auf Aus, mit Erfolg/Fehler pro Gerät |
| `GET /health` | Webserver erreichbar, öffentlich |

`on: null` bedeutet **unbekannt**, nicht „aus“. Doppelte Alias-Namen blockieren Einzel-Toggles. Während eines Cloud-Aufrufs werden weitere Cloud-Anfragen mit 409 abgewiesen; der Webserver bleibt erreichbar. Nach unbestätigten Schreibzugriffen gibt es keinen automatischen Toggle-Retry. Master Off kann bei nicht erreichbaren Geräten nur teilweise erfolgreich sein und meldet dies ausdrücklich. Der sichtbare Live-Tab aktualisiert alle 45 Sekunden; im Hintergrund pausiert die Abfrage.

## Grenzen dieses Tests

- Verwendet die geprüfte Bibliotheksversion `tplink-cloud-api==5.2.2`, deren Projekt Tapo-Cloud-Steuerung dokumentiert. Die tatsächliche Funktion hängt von Modell, Firmware und TP-Link-Cloud ab; ein Test mit echten Geräten ist erforderlich. [Bibliothek](https://github.com/piekstra/tplink-cloud-api).
- Bewusst auf Tapo-Steckdosen der P-Serie begrenzt. Kameras und Hubs werden nicht geschaltet. Mehrfachsteckdosen mit einzeln adressierbaren Ausgängen sind hier nicht implementiert.
- Für TP-Link-Konten mit interaktiver MFA ist ein zusätzlicher Anmeldungs- und Token-Speicher-Flow nötig. Der Minimalserver implementiert diesen nicht und meldet Anmeldefehler; er fordert nicht zum Deaktivieren von MFA auf.
- Ein Serverprozess / eine Instanz: `--workers 1` beibehalten, damit die Schalt-Sperre gilt. Kein Mehrbenutzer- oder Dauerbetriebssystem.
- CORS erlaubt standardmäßig jede Origin. Die API bleibt durch den Zugangsschlüssel geschützt. Eine öffentliche HTML-Datei enthält nur die Backend-URL, keine Geheimnisse. Optional `CORS_ORIGINS` auf konkrete Origins begrenzen, durch Kommas getrennt (keine URL-Pfade).
- Tailwind läuft wie gewünscht über das Browser-CDN. Layout und Gestaltung sind zusätzlich direkt im HTML definiert, damit die Oberfläche auch bei CDN-Ausfall lesbar bleibt.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install httpx
.\.venv\Scripts\python.exe -m unittest -v test_main
```

Die Tests nutzen simulierte Geräte und prüfen Zugriffsschutz, CORS, unbekannten Status, Unicode-/Slash-Aliase, doppelte Aliase, Master Off, Teilfehler und konkurrierende Anfragen. Sie kontaktieren keine echten Geräte.
