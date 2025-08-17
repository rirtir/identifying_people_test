# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import uuid
import json
import asyncio

app = FastAPI()

# players: user_id -> {
#   "ws": WebSocket | None,
#   "connected": bool,
#   "in_game_area": bool,
#   "slot_idx": int | None   # None = no slot (観戦/ロビー)
# }
app.state.players = {}
# ordered list of user_ids that currently occupy game slots (before/after start)
app.state.slots = []  # [user_id1, user_id2, ...]
app.state.game_started = False
app.state.lock = asyncio.Lock()  # 保護用（軽い排他）


@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


async def send_safe(ws: WebSocket, msg: str):
    try:
        await ws.send_text(msg)
    except Exception:
        # 送信失敗しても落とさない
        pass


async def broadcast(msg: str):
    # 全接続中の client に送る
    for p in list(app.state.players.values()):
        ws = p.get("ws")
        if ws is not None and p.get("connected"):
            await send_safe(ws, msg)


def slot_label(idx: int) -> str:
    return f"{idx+1}P"


async def notify_slots_update():
    # slot 情報を全員に送る（JSON）
    slots_info = []
    for idx, uid in enumerate(app.state.slots):
        slots_info.append({"slot": slot_label(idx), "uid": uid})
    msg = "SLOTS " + json.dumps(slots_info)
    await broadcast(msg)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # query から uid を取得。なければ新規発行
    query_uid = websocket.query_params.get("uid")
    async with app.state.lock:
        if not query_uid:
            user_id = str(uuid.uuid4())
            # 新規プレイヤー登録（仮状態: ロビー・未接続スロット）
            app.state.players[user_id] = {
                "ws": websocket,
                "connected": True,
                "in_game_area": False,
                "slot_idx": None
            }
            # 送信順：ASSIGN_ID を先に送る
            await send_safe(websocket, f"ASSIGN_ID {user_id}")
        else:
            user_id = query_uid
            # 既存 UID の扱い
            p = app.state.players.get(user_id)
            if p is None:
                # 未登録ユーザー（初めて来たがuidを指定しているケース）
                app.state.players[user_id] = {
                    "ws": websocket,
                    "connected": True,
                    "in_game_area": False,
                    "slot_idx": None
                }
                await send_safe(websocket, f"ASSIGN_ID {user_id}")
            else:
                # 再接続：ws を差し替え、connected True にする
                p["ws"] = websocket
                p["connected"] = True
                # ASSIGN_ID は重複送信不要だがあってもOK
                await send_safe(websocket, f"ASSIGN_ID {user_id}")

    # 当接続のループ
    try:
        # inform client it's connected
        await send_safe(websocket, "CONNECTED")
        # 初期スロット情報を送る
        await notify_slots_update()

        while True:
            data = await websocket.receive_text()
            # simple text protocol:
            # ENTER_GAME / ENTER_SPECTATE / LEAVE_GAME / START / PING
            cmd = data.strip().upper()

            async with app.state.lock:
                p = app.state.players.get(user_id)
                if p is None:
                    # ちょっと安全側: 登録されてないなら作る
                    app.state.players[user_id] = {
                        "ws": websocket,
                        "connected": True,
                        "in_game_area": False,
                        "slot_idx": None
                    }
                    p = app.state.players[user_id]

                # PING 用
                if cmd == "PING":
                    await send_safe(websocket, "PONG")
                    continue

                # ENTER_SPECTATE: 観戦エリアへ（slot には触らない）
                if cmd == "ENTER_SPECTATE":
                    p["in_game_area"] = False
                    await send_safe(websocket, "ENTERED SPECTATE")
                    continue

                # ENTER_GAME: ゲームエリアに入るリクエスト
                if cmd == "ENTER_GAME":
                    # ゲーム開始後は、既存参加者のみ復帰可能（それ以外は観戦に誘導）
                    if app.state.game_started:
                        # 既に slot を持っている参加者なら復帰を許可
                        if p["slot_idx"] is not None:
                            p["in_game_area"] = True
                            await send_safe(websocket, f"JOINED {slot_label(p['slot_idx'])}")
                        else:
                            # 新規参加不可（観戦へ）
                            await send_safe(websocket, "ONLY_SPECTATOR")
                            # ここでは接続を切らずクライアント側でリダイレクトさせる想定
                        continue
                    # ゲーム未開始の通常入室処理：
                    # もし既に slots に入っている（＝先に入っていて再接続したケース）は復帰
                    if p["slot_idx"] is not None:
                        # すでにどこかのスロットに入っている（通常はないが安全のため）
                        p["in_game_area"] = True
                        await send_safe(websocket, f"JOINED {slot_label(p['slot_idx'])}")
                    else:
                        # 新規にスロット割当て（末尾に追加）
                        app.state.slots.append(user_id)
                        new_idx = len(app.state.slots) - 1
                        p["slot_idx"] = new_idx
                        p["in_game_area"] = True
                        await send_safe(websocket, f"JOINED {slot_label(new_idx)}")
                        # 全員にスロット更新通知
                        await notify_slots_update()
                    continue

                # LEAVE_GAME: ゲームエリアから抜ける（ゲーム開始前なら slot を削除して繰り上げ）
                if cmd == "LEAVE_GAME":
                    if p["in_game_area"] and p["slot_idx"] is not None:
                        # ゲーム未開始なら slot を削除して繰り上げ
                        if not app.state.game_started:
                            # remove from slots list
                            idx = p["slot_idx"]
                            try:
                                app.state.slots.pop(idx)
                            except Exception:
                                pass
                            # clear this player's slot
                            p["slot_idx"] = None
                            p["in_game_area"] = False
                            # 更新: 他の slot_idx を再計算
                            for new_idx, uid in enumerate(app.state.slots):
                                player_obj = app.state.players.get(uid)
                                if player_obj is not None:
                                    player_obj["slot_idx"] = new_idx
                            await notify_slots_update()
                        else:
                            # ゲーム開始後に抜ける（切断扱いと同じ：in_game_area False だが slot は保持）
                            p["in_game_area"] = False
                            p["connected"] = False
                            p["ws"] = None
                            # 他の参加者に通知
                            await broadcast(f"PLAYER_LEFT {user_id}")
                    else:
                        # そもそもゲームエリアにいない
                        p["in_game_area"] = False
                    continue

                # START: 1P が開始ボタンを押す
                if cmd == "START":
                    # only 1P can start, and must be in game area and connected
                    if p["slot_idx"] == 0 and p["in_game_area"] and not app.state.game_started:
                        app.state.game_started = True
                        # notify all connected clients
                        await broadcast(f"GAME_START {len(app.state.slots)}")
                        # after game start, people in lobby (without slot) cannot enter game area;
                        # spectators remain allowed.
                        continue
                    else:
                        await send_safe(websocket, "START_DENIED")
                        continue

                # Unknown command -> echo
                await send_safe(websocket, f"ECHO {data}")

    except WebSocketDisconnect:
        # 切断時の処理
        async with app.state.lock:
            p = app.state.players.get(user_id)
            if p is None:
                return
            # 切断の種類で処理を分ける
            p["connected"] = False
            p["ws"] = None
            # ゲーム開始前かどうか
            if not app.state.game_started:
                # 切断したプレイヤーがスロットを占有していたら削除して繰り上げ
                if p.get("slot_idx") is not None:
                    idx = p["slot_idx"]
                    try:
                        app.state.slots.pop(idx)
                    except Exception:
                        pass
                    # プレイヤーの slot_idx を None にする（IDは消す）
                    p["slot_idx"] = None
                    p["in_game_area"] = False
                    # 再割り当て（slot_idx を更新）
                    for new_idx, uid in enumerate(app.state.slots):
                        player_obj = app.state.players.get(uid)
                        if player_obj is not None:
                            player_obj["slot_idx"] = new_idx
                    # 通知
                    await notify_slots_update()
                else:
                    # そもそもスロット無し（観戦orロビー）なら何もしない
                    pass
            else:
                # ゲーム開始後の切断は slot を保持（復帰可能）
                # なのでここでは connected=False にしておくだけでOK
                await broadcast(f"PLAYER_DISCONNECTED {user_id}")
        return


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=10000)
