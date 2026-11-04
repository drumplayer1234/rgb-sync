// RGB Sync: reports Geometry Dash state to the RGB Sync desktop app.
//
// One JSON message per UDP datagram to 127.0.0.1:47831 (format: docs/gd-protocol.md).
// This mod only observes the game. It never changes gameplay and touches nothing
// but that one loopback socket.

// winsock2.h has to come before anything that pulls in windows.h (the Geode headers do).
#include <winsock2.h>

#include <Geode/Geode.hpp>
#include <Geode/modify/PauseLayer.hpp>
#include <Geode/modify/PlayLayer.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <string>

using namespace geode::prelude;

namespace rgb {
    using Clock = std::chrono::steady_clock;

    constexpr u_short kPort = 47831;
    constexpr auto kStateInterval = std::chrono::milliseconds(33); // about 30 Hz
    constexpr size_t kMaxDatagram = 511;                           // protocol: under 512 bytes
    constexpr size_t kMaxNameChars = 64;

    SOCKET g_socket = INVALID_SOCKET;
    sockaddr_in g_target{};
    bool g_ready = false;
    bool g_failed = false;
    Clock::time_point g_lastState{};
    bool g_deathSent = false;    // one death per attempt
    bool g_completeSent = false; // one complete per attempt

    // ---- networking -------------------------------------------------------

    // Fire and forget: lazily creates a non-blocking UDP socket, ignores every error.
    void post(std::string const& json) {
        if (g_failed || json.size() > kMaxDatagram) return;
        if (!g_ready) {
            WSADATA wsa;
            if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
                g_failed = true;
                return;
            }
            g_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
            if (g_socket == INVALID_SOCKET) {
                g_failed = true;
                return;
            }
            u_long nonBlocking = 1;
            ioctlsocket(g_socket, FIONBIO, &nonBlocking);
            g_target.sin_family = AF_INET;
            g_target.sin_port = htons(kPort);
            g_target.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            g_ready = true;
        }
        sendto(g_socket, json.data(), static_cast<int>(json.size()), 0,
               reinterpret_cast<sockaddr const*>(&g_target), sizeof(g_target));
    }

    // ---- JSON helpers -----------------------------------------------------

    char const* tf(bool value) { return value ? "true" : "false"; }

    float finite(float value) { return std::isfinite(value) ? value : 0.f; }

    // JSON string body for a level name: at most 64 characters, quotes and
    // backslashes escaped, control characters become spaces and non-ASCII bytes
    // become '?', so the output is always valid UTF-8 and one byte per character.
    std::string escapeName(std::string const& name) {
        std::string out;
        for (size_t i = 0; i < name.size() && i < kMaxNameChars; ++i) {
            auto c = static_cast<unsigned char>(name[i]);
            if (c == '"' || c == '\\') {
                out += '\\';
                out += static_cast<char>(c);
            } else if (c < 0x20 || c == 0x7f) {
                out += ' ';
            } else if (c >= 0x80) {
                out += '?';
            } else {
                out += static_cast<char>(c);
            }
        }
        return out;
    }

    // `extra` is empty or starts with a comma, e.g. ,"pct":12.3,"new_best":true
    void sendEvent(char const* type, std::string const& extra = "") {
        post(fmt::format(R"({{"v":1,"t":"{}"{}}})", type, extra));
    }

    // ---- reading the game -------------------------------------------------

    char const* modeOf(PlayerObject* p) {
        if (p->m_isShip) return "ship";
        if (p->m_isBall) return "ball";
        if (p->m_isBird) return "ufo";
        if (p->m_isDart) return "wave";
        if (p->m_isRobot) return "robot";
        if (p->m_isSpider) return "spider";
        if (p->m_isSwing) return "swing";
        return "cube";
    }

    char const* difficultyOf(GJGameLevel* level) {
        if (level->m_demon.value() > 0) return "demon";
        if (level->m_autoLevel) return "auto";
        switch (level->m_stars.value()) {
            case 1: return "auto";
            case 2: return "easy";
            case 3: return "normal";
            case 4: case 5: return "hard";
            case 6: case 7: return "harder";
            case 8: case 9: return "insane";
            case 10: return "demon";
            default: return "unrated";
        }
    }

    // Progress 0-100. Platformer levels have no percent.
    float percentOf(PlayLayer* pl) {
        if (pl->m_isPlatformer) return 0.f;
        return std::clamp(finite(pl->getCurrentPercent()), 0.f, 100.f);
    }

    // Stored best (whole percent) for the current mode, practice or normal.
    // The normal best is an obfuscated "seed value"; value() decodes it.
    int bestOf(PlayLayer* pl) {
        if (pl->m_isPlatformer || !pl->m_level) return 0;
        return pl->m_isPracticeMode ? pl->m_level->m_practicePercent
                                    : pl->m_level->m_normalPercent.value();
    }

    // Would reaching `percent` now beat the stored best? Must be asked BEFORE the
    // original destroyPlayer/levelComplete runs, because those save the new best.
    bool beatsBest(PlayLayer* pl, int percent) {
        return !pl->m_isPlatformer && !pl->m_isTestMode && percent > bestOf(pl);
    }

    // `paused` is passed in because the pause menu has its own tick (see RgbPauseLayer).
    void sendState(PlayLayer* pl, bool paused) {
        auto p1 = pl->m_player1; // dual mode: report player 1
        if (!p1) return;

        auto now = Clock::now();
        if (now - g_lastState < kStateInterval) return;
        g_lastState = now;

        // Background color channel 1000.
        ccColor3B bg{0, 0, 0};
        if (pl->m_effectManager) bg = pl->m_effectManager->activeColorForIndex(1000);

        // Music level used by GD's pulse triggers; only updated while metering is on.
        float pulse = 0.f;
        if (auto audio = FMODAudioEngine::get(); audio && audio->m_metering) {
            pulse = std::clamp(finite(audio->m_pulse1), 0.f, 1.f);
        }

        float speed = p1->m_playerSpeed > 0.f ? p1->m_playerSpeed : 1.f;
        ccColor3B c = p1->m_playerColor1;

        post(fmt::format(
            R"({{"v":1,"t":"state","pct":{:.1f},"best":{:.1f},"mode":"{}","flip":{},"speed":{:.2f},)"
            R"("bg":[{},{},{}],"p1":[{},{},{}],"pulse":{:.2f},"practice":{},"paused":{}}})",
            percentOf(pl), static_cast<float>(bestOf(pl)), modeOf(p1), tf(p1->m_isUpsideDown), finite(speed),
            int(bg.r), int(bg.g), int(bg.b), int(c.r), int(c.g), int(c.b),
            pulse, tf(pl->m_isPracticeMode), tf(paused)));
    }
}

class $modify(RgbPlayLayer, PlayLayer) {
    bool init(GJGameLevel* level, bool dontCreateObjects) {
        if (!PlayLayer::init(level, dontCreateObjects)) return false;
        rgb::g_deathSent = false;
        rgb::g_completeSent = false;
        if (level) {
            rgb::sendEvent("level_start", fmt::format(
                R"(,"name":"{}","stars":{},"difficulty":"{}","platformer":{})",
                rgb::escapeName(level->m_levelName), level->m_stars.value(),
                rgb::difficultyOf(level), rgb::tf(level->isPlatformer())));
        }
        return true;
    }

    void postUpdate(float dt) {
        PlayLayer::postUpdate(dt);
        rgb::sendState(this, m_isPaused);
    }

    void destroyPlayer(PlayerObject* player, GameObject* object) {
        // GD calls this with an invisible anti-cheat spike at the start of every level.
        bool antiCheatProbe = object && object == m_anticheatSpike;

        // The original saves a new best, so decide new_best first.
        int percent = static_cast<int>(rgb::percentOf(this));
        bool newBest = rgb::beatsBest(this, percent);

        PlayLayer::destroyPlayer(player, object);

        // With noclip the original may leave the player alive: only report a real death,
        // and only once per attempt.
        if (antiCheatProbe || rgb::g_deathSent) return;
        bool dead = (m_player1 && m_player1->m_isDead) || (m_player2 && m_player2->m_isDead);
        if (!dead) return;
        rgb::g_deathSent = true;
        rgb::sendEvent("death", fmt::format(R"(,"pct":{:.1f},"new_best":{})",
                                            rgb::percentOf(this), rgb::tf(newBest)));
    }

    void resetLevel() {
        PlayLayer::resetLevel();
        rgb::g_deathSent = false;
        rgb::g_completeSent = false;
        rgb::sendEvent("restart");
    }

    void levelComplete() {
        bool newBest = rgb::beatsBest(this, 100);
        PlayLayer::levelComplete();
        if (rgb::g_completeSent) return;
        rgb::g_completeSent = true;
        rgb::sendEvent("complete", fmt::format(R"(,"new_best":{})", rgb::tf(newBest)));
    }

    CheckpointObject* markCheckpoint() {
        auto checkpoint = PlayLayer::markCheckpoint();
        if (checkpoint) rgb::sendEvent("checkpoint");
        return checkpoint;
    }

    void onQuit() {
        rgb::sendEvent("level_exit");
        PlayLayer::onQuit();
    }
};

// The game scheduler keeps running while the pause menu is open, but the level's own
// update does not, so the pause menu sends the state itself (otherwise the app would
// see "no state for 3 seconds" and think we left the level).
class $modify(RgbPauseLayer, PauseLayer) {
    void customSetup() {
        PauseLayer::customSetup();
        this->schedule(schedule_selector(RgbPauseLayer::rgbTick), 1.f / 30.f);
    }

    void rgbTick(float) {
        if (auto pl = PlayLayer::get()) rgb::sendState(pl, true);
    }
};
