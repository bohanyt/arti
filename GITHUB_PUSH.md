# Push ke GitHub (checklist)

## 1. Rahasia

```powershell
cd <repo-root>
git status
```

Jangan commit: `.env`, `vts_token.txt`, `ARTI_SOUL.md`, `vault/sessions/*.md`

## 2. History bersih (wajib untuk repo publik pertama)

Folder ini sudah disanitasi. **Jangan push `.git` lama** jika pernah berisi kunci/email pribadi.

```powershell
Remove-Item -Recurse -Force .git
git init
git add .
git commit -m "Initial public release — VTuber co-host bridge"
git branch -M main
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin main
```

## 3. Rotate API keys

Jika kunci pernah ada di commit lama (sebelum sanitasi), rotate di konsol Groq / Gemini / OpenRouter.

## 4. Test

```powershell
python -m pytest tests/ -q --ignore=tests/test_supertone_integration.py
```
