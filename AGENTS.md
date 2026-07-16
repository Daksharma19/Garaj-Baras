# Garaj Baras

**Before exploring the codebase, read [ARCHITECTURE.md](ARCHITECTURE.md).**
It is the complete, up-to-date architecture reference: what the project does,
every backend module's role, the radar processing pipeline, all API endpoints,
the caching/LRU memory design, the frontend structure, and deployment. Do not
re-explore the repo to understand the system — only open source files when you
need the exact implementation of a specific function.

Quick facts:
- Backend: FastAPI in `backend/` (run `uvicorn main:app --reload --port 8000` from inside `backend/` with its venv). Hosted on Render free tier — **512 MB RAM is the hard constraint** behind most design choices.
- Frontend: React 19 + Vite in `frontend/` (`npm run dev`). Almost all UI is in `src/App.jsx`.
- Ignore: `backend/debug_*.png`, `misc/`, `frontend/src/NetworkLayers.jsx` (unused), `frontend/dist/` (committed build), the two root `*.txt` summaries (stale — ARCHITECTURE.md supersedes them).
- If you change the architecture (new module, endpoint, radar, or a design change like caching/memory strategy), update ARCHITECTURE.md in the same change.
