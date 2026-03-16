import websockets
import os
import asyncio
import websockets
import json

waiting_player = None

async def handler(websocket):
    global waiting_player
    print("有一个新连接...")

    try:
        if waiting_player is None:
            # 第一个人进来，让他执红方并等待
            waiting_player = websocket
            await websocket.send(json.dumps({"type": "init", "side": "r", "message": "你是红方，等待对手加入..."}))
            print("玩家1(红)已加入，等待玩家2...")
            await websocket.wait_closed()
        else:
            # 第二个人进来，配对成功，他执黑方
            player1 = waiting_player
            player2 = websocket
            waiting_player = None # 清空排队，让后面的人可以继续配对
            
            print("玩家2(黑)加入，游戏开始！")

            await player1.send(json.dumps({"type": "start", "opponent": "已连接"}))
            await player2.send(json.dumps({"type": "init", "side": "b", "message": "你是黑方，游戏开始！"}))

            # 互相转发消息
            await asyncio.gather(
                forward_messages(player1, player2),
                forward_messages(player2, player1)
            )

    except websockets.exceptions.ConnectionClosed:
        print("有人断开了连接")
        if waiting_player == websocket:
            waiting_player = None

async def forward_messages(sender, receiver):
    try:
        async for message in sender:
            await receiver.send(message)
    except:
        try:
            await sender.send(json.dumps({"type": "error", "message": "对手已断线"}))
        except:
            pass

async def main():
    # 云端平台会自动分配一个 PORT 环境变量，如果没有就默认用 8765
    port = int(os.environ.get("PORT", 8765))
    print(f"服务器运行在 0.0.0.0:{port}")
    
    # 注意：这里必须改成 "0.0.0.0"，允许所有外部网络访问
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    asyncio.run(main())


