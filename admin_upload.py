# admin_upload.py
import streamlit as st
import pandas as pd
import base64, json, os
from io import BytesIO, StringIO
import requests

st.set_page_config(page_title="Admin — Upload & Publish", layout="wide")
st.title("Admin: Upload source Excel/CSV and publish processed pivot to GitHub")

# ---------- CONFIG ----------
GITHUB_OWNER = "VeganSindhu"
GITHUB_REPO = "admin_upload"

TARGET_PATH = "processed_pivot.csv"  # file path inside repo
BRANCH = "main"

# GitHub token — prefer using Streamlit secrets for deployment
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    st.warning("GITHUB_TOKEN env var not found. Set environment variable before using admin publish.")
    st.stop()

# ---------- File uploader ----------
uploaded_file = st.file_uploader("Upload Excel (.xlsx single/multi-sheet) or CSV (pivot) - admin only", type=["xlsx","csv"])
if not uploaded_file:
    st.info("Upload the original Excel or the pivot CSV to process and publish.")
    st.stop()

# ---------- PROCESSING: same logic as earlier ----------
def process_uploaded_to_pivot_df(uploaded):
    fname = uploaded.name.lower()
    if fname.endswith(".csv"):
        # simple pivot CSV assumed rows=employees, columns=courses where 1=pending
        df = pd.read_csv(uploaded)
        df.columns = df.columns.astype(str).str.strip()
        df = df.dropna(axis=1, how="all")
        # detect name col (fallback to first)
        possible_name_cols = ["Employee Name", "Name of the Official", "Name", "Employee"]
        name_col = next((c for c in df.columns if c in possible_name_cols), None) or df.columns[0]
        # detect division col
        division_col = next((c for c in df.columns if "division" in c.lower() or "unit" in c.lower()), None)
        exclude = {name_col}
        if division_col:
            exclude.add(division_col)
        for c in df.columns:
            low = c.lower()
            if "s.no" in low or "employee no" in low or "emp no" in low:
                exclude.add(c)
        course_cols = [c for c in df.columns if c not in exclude]
        # create standard pivot: index Employee Name, columns Course Name, values 1 or 0 (pending)
        # normalize pending to 1/0
        pivot = df.set_index(name_col)[course_cols].applymap(lambda x: 1 if str(x).strip() == "1" else 0)
        # ensure Employee Name as a column for saving (not index)
        pivot = pivot.reset_index()
        # add Division column if available
        if division_col:
            div_series = df[[name_col, division_col]].drop_duplicates().set_index(name_col)[division_col]
            pivot["Division/ Unit"] = pivot[name_col].map(div_series)
        return pivot, name_col, course_cols
    else:
        # Excel multi-sheet flow: extract RMS TP rows, add Course Name=sheet
        xls = pd.ExcelFile(uploaded)
        combined = pd.DataFrame()
        for sheet in xls.sheet_names:
            # keep header=1 as your original logic; adjust if your sheets differ
            df_sheet = pd.read_excel(uploaded, sheet_name=sheet, header=1)
            df_sheet.columns = df_sheet.columns.astype(str).str.strip()
            df_sheet = df_sheet.dropna(axis=1, how="all")
            # drop unnamed cols
            df_sheet = df_sheet[[c for c in df_sheet.columns if not str(c).lower().startswith("unnamed")]]
            division_col = next((c for c in df_sheet.columns if "division" in c.lower() or "unit" in c.lower()), None)
            if division_col and division_col in df_sheet.columns:
                df_tp = df_sheet[df_sheet[division_col].astype(str).str.contains("RMS TP", case=False, na=False)]
            else:
                tp_mask = df_sheet.apply(lambda col: col.astype(str).str.contains("RMS TP", case=False, na=False))
                if tp_mask.any().any():
                    df_tp = df_sheet[tp_mask.any(axis=1)]
                else:
                    df_tp = pd.DataFrame()
            if df_tp.empty:
                continue
            df_tp["Course Name"] = sheet
            combined = pd.concat([combined, df_tp], ignore_index=True)
        if combined.empty:
            st.error("No RMS TP rows were found in any sheet.")
            st.stop()
        # Now create pivot: Employee Name x Course Name, value=1 if pending (or count)
        # detect name & emp no cols
        possible_name_cols = ["Employee Name", "Name of the Official", "Name", "Employee"]
        name_col = next((c for c in combined.columns if c in possible_name_cols), None)
        if not name_col:
            st.error("Employee name column missing after consolidation.")
            st.stop()
        # For Excel flow we assume each row is a completion/presence -> mark 1
        combined["PRESENT"] = 1
        pivot = combined.pivot_table(index=name_col, columns="Course Name", values="PRESENT", aggfunc="sum", fill_value=0)
        pivot = pivot.reset_index()
        # attach Division if present
        division_col = next((c for c in combined.columns if "division" in c.lower() or "unit" in c.lower()), None)
        if division_col:
            div_map = combined[[name_col, division_col]].drop_duplicates().set_index(name_col)[division_col]
            pivot["Division/ Unit"] = pivot[name_col].map(div_map)
        # compute course_cols list (exclude name and Division/ Unit if present)
        # pivot.columns may be Index([name_col, course1, course2, ..., 'Division/ Unit'])
        # we'll return course column names (excluding name_col and Division/ Unit)
        cols = [c for c in pivot.columns.tolist() if c != name_col and c != "Division/ Unit"]
        return pivot, name_col, cols

# Run processing
pivot_df, name_col, course_cols = process_uploaded_to_pivot_df(uploaded_file)

st.write("Preview of processed pivot (first 10 rows):")
st.dataframe(pivot_df.head(10))

# ---------- Save pivot to bytes (CSV) ----------
csv_bytes = pivot_df.to_csv(index=False).encode("utf-8")

# ---------- GitHub: get existing file sha (if exists) ----------
api_base = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{TARGET_PATH}"
headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "admin-upload-script",
    "X-GitHub-Api-Version": "2022-11-28",
}

# get existing file to obtain sha (for update)
resp_get = requests.get(api_base, headers=headers, params={"ref": BRANCH})
st.write("GET file response status:", resp_get.status_code)
try:
    get_json = resp_get.json()
    st.write("GET file response json:", get_json)
except ValueError:
    get_json = None
    st.write("GET file response text:", resp_get.text)

if resp_get.status_code == 200 and get_json:
    sha = get_json.get("sha")
    st.write(f"Existing file found. sha={sha}")
elif resp_get.status_code == 404:
    sha = None
    st.write("File does not exist yet (404). Will create a new file.")
else:
    st.error(f"Failed to fetch existing file info: {resp_get.status_code}")
    if get_json:
        st.write(get_json)
    else:
        st.write(resp_get.text)
    st.stop()

# prepare payload (base64)
content_b64 = base64.b64encode(csv_bytes).decode("utf-8")
payload = {
    "message": "Admin: update processed pivot",
    "content": content_b64,
    "branch": BRANCH
}
if sha:
    payload["sha"] = sha

# show payload metadata (not content) for debug
st.write("Preparing to PUT to GitHub — branch:", BRANCH, "sha:", sha, "size(bytes):", len(csv_bytes))

# Use json=payload so requests sets proper Content-Type
resp_put = requests.put(api_base, headers=headers, json=payload)

st.write("PUT response status:", resp_put.status_code)
try:
    put_json = resp_put.json()
    st.write("PUT response json:", put_json)
except ValueError:
    put_json = None
    st.write("PUT response text:", resp_put.text)

if resp_put.status_code in (200, 201):
    st.success("Processed pivot successfully uploaded to GitHub.")
    html_url = put_json["content"]["html_url"]
    st.write("File URL:", html_url)
    raw_url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{BRANCH}/{TARGET_PATH}"
    st.write("Raw CSV URL (use in user app):", raw_url)
else:
    st.error("Failed to upload to GitHub. See details below.")
    st.write("Status code:", resp_put.status_code)
    if put_json:
        st.write(put_json)
        msg = put_json.get("message", "")
        # Helpful hints
        if "Bad credentials" in msg or resp_put.status_code == 401:
            st.error("Authentication failed. Check the token and its scopes (repo/public_repo).")
        elif resp_put.status_code == 422:
            st.error("Unprocessable entity (422) — likely wrong sha, branch or file path. Try removing sha to create file or fetch latest sha.")
        elif resp_put.status_code == 403:
            st.error("Forbidden — token might be missing required scopes or repo access is restricted.")
        else:
            st.info("See JSON above for more details.")
    else:
        st.write(resp_put.text)
    st.stop()
