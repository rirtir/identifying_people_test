# server.py
import os, uuid, json, asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn


app = FastAPI()

# players: uid -> {
#   "ws": WebSocket | None,
#   "connected": bool,
#   "in_game_area": bool,
#   "slot_idx": int | None   # None = no slot (観戦/ロビー)
# }
app.state.players = {}
# ordered list of uids that currently occupy game slots (before/after start)
app.state.slots = []  # [uid1, uid2, ...]
app.state.game_started = False
app.state.lock = asyncio.Lock()  # 保護用（軽い排他）
# このスクリプト自身のディレクトリを取得
app.state.base_dir = os.path.dirname(os.path.abspath(__file__))

@app.get("/")
async def get():
    with open(os.path.join(app.state.base_dir, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


async def send_safe(ws: WebSocket, message: dict):
    try:
        await ws.send_text(json.dumps(message))
    except Exception:
        # 送信失敗しても落とさない
        pass


async def send_safe_key(ws: WebSocket, type: str, key: str=None, value=None):
    if key is None:
        await send_safe(ws, {"type": type})
    else:
        await send_safe(ws, {"type": type, key: value})


async def broadcast(type: str, key: str, msg: str):
    # 全接続中の client に送る
    for p in list(app.state.players.values()):
        ws = p.get("ws")
        if ws is not None and p.get("connected"):
            await send_safe(ws, {"type": type, key: msg})


def slot_label(idx: int) -> str:
    return f"{idx+1}P"


async def notify_slots_update():
    # slot 情報を全員に送る（JSON）
    slots_info = []
    for idx, uid in enumerate(app.state.slots):
        slots_info.append({"slot": slot_label(idx), "uid": uid})
    await broadcast("SLOTS", "slots_info", slots_info)


def register_player(uid, ws: WebSocket, connect: bool, game_area: bool, watch_area: bool, slot):
    app.state.players[uid] = {
        "ws": ws,
        "connected": connect,
        "in_game_area": game_area,
        "in_watch_area": watch_area,
        "slot_idx": slot,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # query から uid を取得。なければ新規発行
    query_uid = websocket.query_params.get("uid")
    async with app.state.lock:
        if not query_uid:
            uid = str(uuid.uuid4())
            # 新規プレイヤー登録（仮状態: ロビー・未接続スロット）
            register_player(uid=uid, ws=websocket, connect=True, game_area=False, watch_area=False, slot=None)
            # 送信順：ASSIGN_ID を先に送る
            await send_safe_key(websocket, type="ASSIGN_ID", key="user_id", value=uid)
        else:
            uid = query_uid
            # 既存 UID の扱い
            p = app.state.players.get(uid)
            if p is None:
                # 未登録ユーザー（初めて来たがuidを指定しているケース）
                register_player(uid=uid, ws=websocket, connect=True, game_area=False, watch_area=False, slot=None)
                await send_safe_key(websocket, type="ASSIGN_ID", key="user_id", value=uid)
            else:
                # 再接続：ws を差し替え、connected True にする
                p["ws"] = websocket
                p["connected"] = True
                # ASSIGN_ID は重複送信不要だがあってもOK
                await send_safe_key(websocket, type="ASSIGN_ID", key="user_id", value=uid)

    # 当接続のループ
    try:
        # inform client it's connected
        await send_safe_key(websocket, type="CONNECTED")
        # 初期スロット情報を送る
        await notify_slots_update()

        while True:
            data = await websocket.receive_text()
            # simple text protocol:
            # ENTER_GAME / ENTER_SPECTATE / LEAVE_GAME / START / PING
            msg = json.loads(data)

            async with app.state.lock:
                p = app.state.players.get(uid)
                if p is None:
                    # ちょっと安全側: 登録されてないなら作る
                    register_player(uid=uid, ws=websocket, connect=True, game_area=False, watch_area=False, slot=None)
                    p = app.state.players[uid]

                # PING 用
                if msg["type"] == "PING":
                    await send_safe_key(websocket, type="PONG")
                    continue

                # ENTER_SPECTATE: 観戦エリアへ（slot には触らない）
                if msg["type"] == "ENTER_SPECTATE":
                    p["in_game_area"] = False
                    p["in_watch_area"] = True
                    p["slot_idx"] = None
                    await send_safe_key(websocket, type="ENTERED_SPECTATE")
                    continue

                # ENTER_GAME: ゲームエリアに入るリクエスト
                if msg["type"] == "ENTER_GAME":
                    # ゲーム開始後は、既存参加者のみ復帰可能（それ以外は観戦に誘導）
                    if app.state.game_started:
                        # 既に slot を持っている参加者なら復帰を許可
                        if p["slot_idx"] is not None:
                            p["in_game_area"] = True
                            p["in_watch_area"] = False
                            await send_safe_key(websocket, "JOINED", "slot", slot_label(p['slot_idx']))
                        else:
                            # 新規参加不可（観戦へ）
                            p["in_game_area"] = False
                            p["in_watch_area"] = True
                            p["slot_idx"] = None
                            await send_safe_key(websocket, type="ONLY_SPECTATOR")
                            # ここでは接続を切らずクライアント側でリダイレクトさせる想定
                        continue
                    # ゲーム未開始の通常入室処理：
                    # もし既に slots に入っている（＝先に入っていて再接続したケース）は復帰
                    if p["slot_idx"] is not None:
                        # すでにどこかのスロットに入っている（通常はないが安全のため）
                        p["in_game_area"] = True
                        p["in_watch_area"] = False
                        await send_safe_key(websocket, "JOINED", "slot", slot_label(p['slot_idx']))
                    else:
                        # 新規にスロット割当て（末尾に追加）
                        app.state.slots.append(uid)
                        new_idx = len(app.state.slots) - 1
                        p["slot_idx"] = new_idx
                        p["in_game_area"] = True
                        p["in_watch_area"] = False
                        await send_safe_key(websocket, "JOINED", "slot", slot_label(new_idx))
                        # 全員にスロット更新通知
                        await notify_slots_update()
                    continue

                # LEAVE_GAME: ゲームエリアから抜ける（ゲーム開始前なら slot を削除して繰り上げ）
                if msg["type"] == "LEAVE_GAME":
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
                            await broadcast("PLAYER_LEFT", "user_id", uid)
                    else:
                        # そもそもゲームエリアにいない
                        p["in_game_area"] = False
                    continue

                # START: 1P が開始ボタンを押す
                if msg["type"] == "START":
                    # only 1P can start, and must be in game area and connected
                    if p["slot_idx"] == 0 and p["in_game_area"] and not app.state.game_started:
                        app.state.game_started = True
                        # notify all connected clients
                        await broadcast(f"GAME_START {len(app.state.slots)}")
                        # after game start, people in lobby (without slot) cannot enter game area;
                        # spectators remain allowed.
                        continue
                    else:
                        await send_safe_key(websocket, "START_DENIED")
                        continue

                # Unknown command -> echo
                await send_safe_key(websocket, "ECHO", "data", data)

    except WebSocketDisconnect:
        # 切断時の処理
        async with app.state.lock:
            p = app.state.players.get(uid)
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
                    p["in_watch_area"] = False
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
                await broadcast("PLAYER_DISCONNECTED", "user_id", uid)
        return


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=10000)
