from fastapi import FastAPI
import uvicorn
from db import get_async_session
from sqlalchemy import text

app = FastAPI()

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.get("/session_test")
async def test():
    async with get_async_session() as session:
        rows = await session.execute(text("select * from steam_account"))
        print(rows.mappings().all())
        return {"test": "ok"}

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8081,
        reload=True,
    )