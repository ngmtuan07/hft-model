// Minimal HTTP server for the live scoreboard (no dependencies).
//
//   GET /            the scoreboard page (read from disk on each request, so it can be edited live)
//   GET /events      Server-Sent Events: the latest JSON snapshot, pushed whenever it changes
//   GET /snapshot    the latest JSON snapshot once
//   GET /control?c=<contestant id>&extra_us=<n>   add n µs to that contestant's order path
//   GET /control?reset=1                          reset race / fill / latency statistics
//
// Runs on its own threads. The exchange thread only publishes a snapshot string (under a mutex,
// a few times per second) and reads a handful of atomics, so the trading path never blocks on
// a browser.
#pragma once
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>

namespace web {

constexpr int MAX_CONTESTANTS = 8;

struct Shared {
    std::mutex mu;
    std::string snapshot = "{}";
    uint64_t version = 0;
    std::atomic<int64_t> extra_ns[MAX_CONTESTANTS] = {};   // index = contestant id - 1
    std::atomic<bool> reset{false};
    std::string ui_path;

    void publish(std::string s) {
        std::lock_guard<std::mutex> g(mu);
        snapshot = std::move(s);
        ++version;
    }
    uint64_t get(std::string& out, uint64_t have) {
        std::lock_guard<std::mutex> g(mu);
        if (version != have) out = snapshot;
        return version;
    }
};

class Server {
public:
    Server(int port, Shared* sh) : sh_(sh) {
        std::signal(SIGPIPE, SIG_IGN);
        fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        const int one = 1;
        setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_port = htons(uint16_t(port));
        a.sin_addr.s_addr = htonl(INADDR_ANY);
        if (::bind(fd_, reinterpret_cast<sockaddr*>(&a), sizeof(a)) != 0 || ::listen(fd_, 16) != 0) {
            std::fprintf(stderr, "http: cannot listen on port %d: %s\n", port, std::strerror(errno));
            ::close(fd_);
            fd_ = -1;
            return;
        }
        std::thread([this] { accept_loop(); }).detach();
    }
    bool ok() const { return fd_ >= 0; }

private:
    void accept_loop() {
        while (true) {
            const int c = ::accept(fd_, nullptr, nullptr);
            if (c < 0) continue;
#ifdef SO_NOSIGPIPE
            const int one = 1;
            setsockopt(c, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
            std::thread([this, c] { serve(c); ::close(c); }).detach();
        }
    }

    static bool send_all(int c, const std::string& s) {
        size_t off = 0;
        while (off < s.size()) {
#ifdef MSG_NOSIGNAL
            const ssize_t n = ::send(c, s.data() + off, s.size() - off, MSG_NOSIGNAL);
#else
            const ssize_t n = ::send(c, s.data() + off, s.size() - off, 0);
#endif
            if (n <= 0) return false;
            off += size_t(n);
        }
        return true;
    }

    static std::string response(const char* status, const char* type, const std::string& body) {
        return std::string("HTTP/1.1 ") + status + "\r\nContent-Type: " + type +
               "\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: " + std::to_string(body.size()) +
               "\r\n\r\n" + body;
    }

    static std::string query_value(const std::string& q, const std::string& key) {
        size_t p = 0;
        while (p < q.size()) {
            size_t e = q.find('&', p);
            if (e == std::string::npos) e = q.size();
            const std::string kv = q.substr(p, e - p);
            const size_t eq = kv.find('=');
            if (eq != std::string::npos && kv.substr(0, eq) == key) return kv.substr(eq + 1);
            p = e + 1;
        }
        return "";
    }

    void serve(int c) {
        char buf[4096];
        size_t n = 0;
        while (n < sizeof(buf) - 1) {
            const ssize_t r = ::recv(c, buf + n, sizeof(buf) - 1 - n, 0);
            if (r <= 0) return;
            n += size_t(r);
            buf[n] = 0;
            if (std::strstr(buf, "\r\n\r\n")) break;
        }
        char method[8] = {}, target[1024] = {};
        if (std::sscanf(buf, "%7s %1023s", method, target) != 2 || std::strcmp(method, "GET") != 0) {
            send_all(c, response("405 Method Not Allowed", "text/plain", "GET only\n"));
            return;
        }
        std::string path = target, query;
        const size_t qm = path.find('?');
        if (qm != std::string::npos) { query = path.substr(qm + 1); path = path.substr(0, qm); }

        if (path == "/" || path == "/index.html") {
            FILE* f = std::fopen(sh_->ui_path.c_str(), "rb");
            if (!f) { send_all(c, response("404 Not Found", "text/plain", "scoreboard page not found: " + sh_->ui_path + "\n")); return; }
            std::string body;
            char b[8192];
            size_t k;
            while ((k = std::fread(b, 1, sizeof(b), f)) > 0) body.append(b, k);
            std::fclose(f);
            send_all(c, response("200 OK", "text/html; charset=utf-8", body));
        } else if (path == "/snapshot") {
            std::string s;
            sh_->get(s, ~0ull);
            send_all(c, response("200 OK", "application/json", s));
        } else if (path == "/events") {
            if (!send_all(c, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                             "Connection: keep-alive\r\n\r\n")) return;
            uint64_t have = ~0ull;
            std::string s;
            int idle = 0;
            while (true) {
                const uint64_t v = sh_->get(s, have);
                if (v != have) {
                    have = v;
                    if (!send_all(c, "data: " + s + "\n\n")) return;
                    idle = 0;
                } else if (++idle >= 150) {          // keep-alive comment every ~3 s
                    if (!send_all(c, ": ping\n\n")) return;
                    idle = 0;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
        } else if (path == "/control") {
            std::string msg = "{\"ok\": true}";
            if (query_value(query, "reset") == "1") sh_->reset = true;
            const std::string cs = query_value(query, "c"), us = query_value(query, "extra_us");
            if (!cs.empty() && !us.empty()) {
                const int id = std::atoi(cs.c_str());
                const double v = std::atof(us.c_str());
                if (id >= 1 && id <= MAX_CONTESTANTS && v >= 0 && v <= 1e6) sh_->extra_ns[id - 1] = int64_t(v * 1000.0);
                else msg = "{\"ok\": false, \"error\": \"bad contestant or value\"}";
            }
            send_all(c, response("200 OK", "application/json", msg));
        } else {
            send_all(c, response("404 Not Found", "text/plain", "not found\n"));
        }
    }

    Shared* sh_;
    int fd_ = -1;
};

}  // namespace web
