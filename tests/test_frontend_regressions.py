import re
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


def test_knowledge_base_dialog_is_remounted_for_each_open():
    """The dialog must remain a sibling of the empty-state component."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<el-dialog v-if="kbDialogVisible" v-model="kbDialogVisible"' in html
    assert "setTimeout(r, 100)" not in html


def test_element_plus_components_are_not_self_closing():
    """In-DOM Vue templates require explicit closing tags for custom elements."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert re.search(r"<el-[^>]+/>", html) is None


def test_confirmed_logout_immediately_returns_to_auth_screen():
    """Network token revocation must not block the local login-screen transition."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    logout = html.index("async logout()")
    confirm = html.index("async confirmLogout()", logout)
    block = html[logout:confirm]

    assert "const revokeRequest = fetch('/api/auth/logout'" in block
    assert block.index("this.forceLogout()") < block.index("await revokeRequest")
    assert "this.authTab = 'login'" in block
    assert "this.authForm = { username: '', phone: '', password: '' }" in block

    confirm_block = html[confirm : html.index("async bootstrap()", confirm)]
    assert "catch { return; }" in confirm_block
    assert confirm_block.index("catch { return; }") < confirm_block.index("await this.logout()")


def test_auth_request_has_a_hard_timeout_and_always_stops_loading():
    html = INDEX_HTML.read_text(encoding="utf-8")
    auth = html[html.index("async doAuth()") : html.index("async logout()")]

    assert "if (this.authLoading) return" in auth
    assert "setTimeout(() => controller.abort(), 10000)" in auth
    assert "signal: controller.signal" in auth
    assert "clearTimeout(timeoutId)" in auth
    assert "this.authLoading = false" in auth


def test_chat_requires_a_concrete_knowledge_base_selection():
    html = INDEX_HTML.read_text(encoding="utf-8")
    ask = html[html.index("async ask(preset)") : html.index("const bot = Vue.reactive", html.index("async ask(preset)"))]

    assert 'label="全部知识库"' not in html
    assert 'placeholder="请选择知识库"' in html
    assert '@change="onChatKbChange"' in html
    assert 'json: { question, conversation_id: this.conversationId, kb_id: this.chatKb || null' in html
    assert "if (!this.chatKb) return ElementPlus.ElMessage.warning('请先选择一个知识库')" in ask
    assert ':disabled="streaming || !chatKb"' in html
    assert ':disabled="!chatKb"' in html


def test_chat_kb_switch_starts_a_fresh_conversation():
    html = INDEX_HTML.read_text(encoding="utf-8")
    handler = html[html.index("onChatKbChange()") : html.index("async loadConversations()", html.index("onChatKbChange()"))]

    assert "this.newChat()" in handler
    assert "已切换知识库，并为你开启新对话" in handler


def test_agent_memory_usage_is_visible_in_chat():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "🧠 已调用 {{ m.memoryCount }} 条跨对话记忆" in html
    assert "case 'memory': bot.memoryCount = data.count" in html
