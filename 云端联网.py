import os
import asyncio
import websockets
import json

# 存放当前房间内的玩家连接
players = {'r': None, 'b': None}

async def handler(websocket):
    global players
    print("新连接建立...")
    my_side = None

    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            # 1. 处理玩家加入并选择阵营
            if msg_type == "join":
                side = data.get("side") # 'r' 或 'b'
                if players.get(side) is None:
                    players[side] = websocket
                    my_side = side
                    await websocket.send(json.dumps({"type": "join_success", "side": side}))
                    print(f"玩家加入了 {side} 方")
                    
                    # 如果双方都到齐了，通知游戏开始
                    if players['r'] and players['b']:
                        await players['r'].send(json.dumps({"type": "start"}))
                        await players['b'].send(json.dumps({"type": "start"}))
                else:
                    # 如果该阵营已经有人了，拒绝加入
                    await websocket.send(json.dumps({"type": "error", "message": "该阵营已被占用，请选择另一方！"}))

            # 2. 转发游戏内的交互指令（走棋、聊天、悔棋、认输等）
            else:
                if my_side:
                    opponent_side = 'b' if my_side == 'r' else 'r'
                    # 如果对手在线，直接把原消息原样转发给对手
                    if players.get(opponent_side):
                        await players[opponent_side].send(message)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # 玩家断开连接时的清理工作
        if my_side and players.get(my_side) == websocket:
            players[my_side] = None
            print(f"{my_side} 方断开了连接")
            opponent_side = 'b' if my_side == 'r' else 'r'
            if players.get(opponent_side):
                try:
                    await players[opponent_side].send(json.dumps({"type": "opponent_left", "message": "对手已断开连接"}))
                except:
                    pass

async def main():
    port = int(os.environ.get("PORT", 8765))
    print(f"服务器运行在 0.0.0.0:{port}")
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    asyncio.run(main())
