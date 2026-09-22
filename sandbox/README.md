# Sandbox

Use this folder for exploratory `.ipynb` notebooks. Move code worth reusing into
`src/polytrader/data/` or `src/polytrader/bot/` and import it from your notebooks.

From the repository root, install the optional notebook dependencies:

```powershell
python -m pip install -r requirements-sandbox.txt
```

With uv: `uv pip install -r requirements-sandbox.txt`.

Open `01_history.ipynb` in VS Code and select this repository's `.venv` as the
Python kernel. The notebook locates the repo from either its root or this folder.
It loads an existing JSON file under `data/`, displays metadata, and plots prices.
If `lepen-history-7d.json` exists, it is selected first; otherwise the notebook
shows which saved file it selected. Edit `history_path` to choose a different file.

Loading and plotting are offline. The final cell contains commented examples of
fetching fresh data; only uncomment them when you want an API request. Save data
under the root `data/` directory. Clear notebook outputs before committing them.
