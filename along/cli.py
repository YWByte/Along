import argparse
import logging
import os
import sys

from along.config import Settings


def main():
    parser = argparse.ArgumentParser(description="ALONG：本地即时图像问答和长程任务陪伴")
    parser.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "prepare-speech", "prepare-embeddings"]
    )
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for logger in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(logger).setLevel(logging.WARNING)
    settings = Settings()
    os.umask(0o077)
    if args.command == "prepare-speech":
        from faster_whisper import WhisperModel

        print(f"下载并验证本地语音模型 {settings.whisper_model}，首次运行需要网络。", flush=True)
        WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
        print("语音模型就绪。", flush=True)
    elif args.command == "prepare-embeddings":
        from sentence_transformers import SentenceTransformer

        print(f"下载并验证本地语义模型 {settings.embedding_model}。", flush=True)
        model = SentenceTransformer(settings.embedding_model)
        assert model.encode(["滑轨装反", "面团过湿"]).shape[0] == 2
        print("语义模型就绪；设置 ALONG_SEMANTIC_ENABLED=true 启用。", flush=True)
    else:
        import fcntl
        import uvicorn
        from along.api import create_app

        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (settings.data_dir / "server.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("同一数据目录已有 ALONG 服务，拒绝重复运行后台处理器。")
        print(f"ALONG http://127.0.0.1:{args.port or settings.port} | 数据 {settings.data_dir}", flush=True)
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port or settings.port)
        lock.close()


if __name__ == "__main__":
    main()
