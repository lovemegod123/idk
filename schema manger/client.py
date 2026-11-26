import socket
import sys
import time

HOST = '127.0.0.1'
PORT = 65432

messages = [
    "Hello server!",
    "This is a test message.",
    "Are you there?",
    "quit",
]

def main():
    time.sleep(1)  # let server start
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        print(f"[CLIENT] Connected to {HOST}:{PORT}")
        for msg in messages:
            s.sendall(msg.encode())
            data = s.recv(1024)
            print(f"[CLIENT] Received: {data.decode()}")
            if msg.strip().lower() == 'quit':
                break
            time.sleep(1)
        print("[CLIENT] Done.")

if __name__ == '__main__':
    main()