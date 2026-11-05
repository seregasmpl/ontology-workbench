# Сторонние компоненты

Backend использует Python, FastAPI, Starlette, Pydantic, HTTPX, Uvicorn, keyring, python-multipart и Apache Arrow. Интерфейс использует React, React DOM, scheduler и lucide-react. Переносимая Windows-сборка создаётся PyInstaller.

Конкретные версии Python-пакетов проверенной среды находятся в `requirements-lock.txt`, JavaScript-пакетов — в `frontend/package-lock.json`. Сведения об авторских правах и лицензиях компонентов сохраняются в каталоге `THIRD_PARTY_LICENSES` переносимой сборки. Этот каталог генерирует `tools/collect_licenses.py` из файлов установленных зависимостей; оригинальные уведомления внутри зависимостей также сохраняются.

Лицензии сторонних компонентов не определяют условия использования собственного кода этого проекта. Для собственного кода отдельная лицензия в этой предварительной публикации не выбрана.
