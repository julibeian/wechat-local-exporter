"""Browser regressions for offline archives (optional Playwright + local Edge)."""
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from wechat_exporter.chat_html_archive import ChatArchiveMedia, export_chat_html_archive
from wechat_exporter.models import Conversation, Message

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(channel="msedge", headless=True)
        except playwright.Error as error:
            pytest.skip(f"Local Edge is unavailable: {error}")
        yield instance
        instance.close()


@pytest.fixture
def archive(tmp_path):
    def build(group=False):
        conversation = Conversation("group@chatroom" if group else "friend", "交互验收", is_group=group)
        pictures = {1, 169, 171, 341}
        messages = [Message(i, 1788307200 + i, 3 if i in pictures else 1,
                            "member-0" if i in pictures else f"member-{i % 30}",
                            "张三" if i in pictures else f"成员{i % 30}", False,
                            "[图片]" if i in pictures else "原生[西瓜][吃瓜][握手][咖啡]和🍉；[未知]",
                            conversation_id=conversation.username) for i in range(1, 342)]
        def resolve(message):
            pixels = Image.new("RGB", (120, 180), (message.local_id % 256, 120, 180))
            stream = io.BytesIO()
            pixels.save(stream, format="PNG")
            return ChatArchiveMedia("image", data=stream.getvalue(), extension="png")
        result = export_chat_html_archive(
            root=tmp_path / ("group" if group else "private"), conversation=conversation,
            messages=messages, media_resolver=resolve, chunk_size=170,
            profiles={"member-0": SimpleNamespace(group_alias="小张（群内昵称）", remark="张三", nickname="张老师")})
        records = []
        for chunk in sorted((result.root / "data").glob("messages-*.js")):
            text = chunk.read_text(encoding="utf-8")
            records.extend(json.loads(text[text.index(",") + 1:text.rindex(")")]))
        return result, records
    return build


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    context.close()
    assert not errors


def load(page, result):
    page.goto(result.entrypoint.as_uri())
    playwright.expect(page.locator(".message-row")).to_have_count(1)


@pytest.mark.parametrize("group", [False, True])
def test_member_name_search_includes_group_nickname_and_contact_name(page, archive, group):
    result, _ = archive(group)
    load(page, result)
    page.locator("#member-filter-trigger").click()
    search = page.locator("#member-filter-search")
    search.fill("张三")
    playwright.expect(page.locator("#member-filter-options .filter-option:visible")).to_have_count(1)
    search.fill("张老师")
    playwright.expect(page.locator("#member-filter-options .filter-option:visible")).to_have_count(1)
    search.fill("不存在")
    playwright.expect(page.locator("#member-filter-menu .filter-empty")).to_be_visible()
    search.press("ArrowDown")  # Empty results must not break keyboard navigation.
    search.fill("张三")
    search.press("ArrowDown")
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#search-count")).to_have_text("15")
    playwright.expect(page.locator("#member-filter-menu")).to_be_hidden()
    assert page.locator("#member-filter").input_value() == "member-0"
    page.locator("#clear-button").click()
    page.locator("#member-filter-trigger").click()
    assert search.input_value() == ""
    playwright.expect(page.locator("#member-filter-options .filter-option:visible")).to_have_count(31)


def test_photo_navigation_crosses_chunks_and_virtual_windows_then_closes(page, archive):
    result, records = archive()
    load(page, result)
    paths = [record["media"][0]["path"] for record in records if record["type"] == "image"]
    page.locator(".media-open").click()
    image = page.locator("#dialog-image")
    playwright.expect(image).to_have_attribute("src", paths[-1])
    playwright.expect(page.locator("#media-next")).to_be_disabled()
    for path in reversed(paths[:-1]):
        page.locator("#media-previous").click()
        playwright.expect(image).to_have_attribute("src", path)
        if path != paths[0]:
            playwright.expect(page.locator("#media-previous")).to_be_enabled()
    playwright.expect(page.locator("#media-previous")).to_be_disabled()
    for path in paths[1:]:
        page.keyboard.press("ArrowRight")
        playwright.expect(image).to_have_attribute("src", path)
    image.click()
    playwright.expect(page.locator("#media-dialog")).to_be_visible()
    page.locator("#media-viewport").click(position={"x": 4, "y": 4})
    playwright.expect(page.locator("#media-dialog")).to_be_hidden()
    # Viewing other chunks must preserve the original reading position.
    playwright.expect(page.locator(".message-row")).to_have_count(1)
    page.locator(".media-open").click()
    page.get_by_role("button", name="关闭图片", exact=True).click()
    playwright.expect(page.locator("#media-dialog")).to_be_hidden()
    page.locator(".media-open").click()
    page.keyboard.press("Escape")
    playwright.expect(page.locator("#media-dialog")).to_be_hidden()


def test_html_renders_original_faces_and_keeps_unknown_labels_and_unicode(page, archive):
    result, _ = archive()
    load(page, result)
    page.locator("#conversation-pane").focus()
    page.keyboard.press("Control+Home")
    playwright.expect(page.locator(".native-face").first).to_be_visible()
    text = page.locator(".message-text").first
    assert text.locator(".native-face").count() == 4
    assert [image.get_attribute("alt") for image in text.locator(".native-face").all()] == ["[西瓜]", "[吃瓜]", "[握手]", "[咖啡]"]
    assert "🍉" in text.inner_text() and "[未知]" in text.inner_text()
    assert page.locator(".message-row").count() <= 160
    page.wait_for_function("Array.from(document.querySelectorAll('.native-face')).every(img => img.complete && img.naturalWidth > 0)")
