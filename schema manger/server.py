import socket
import threading
import sys

HOST = '127.0.0.1'
PORT = 65432

def handle_client(conn, addr):
    print(f"[SERVER] Connected by {addr}")
    try:
        while True:
            data = conn.recv(1024)
            if not data:
                break
            msg = data.decode()
            print(f"[SERVER] Received from {addr}: {msg}")
            if msg.strip().lower() == 'quit':
                conn.sendall(b"Goodbye!")
                break
            response = f"Echo: {msg}"
            conn.sendall(response.encode())
    except ConnectionResetError:
        pass
    finally:
        conn.close()
        print(f"[SERVER] Disconnected {addr}")

def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen()
        print(f"[SERVER] Listening on {HOST}:{PORT}")
        try:
            while True:
                conn, addr = s.accept()
                t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
                t.start()
        except KeyboardInterrupt:
            print("\n[SERVER] Shutting down")

if __name__ == '__main__':
    main()