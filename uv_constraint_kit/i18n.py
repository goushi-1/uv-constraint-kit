# SPDX-License-Identifier: GPL-3.0-or-later
"""Chinese/English switching for the add-on, driven by Blender's own language.

The strings in `ui.py` / `ops.py` / `prefs.py` are **English sources**; the
Chinese lives in `terms_zh.py`. Nothing is baked into `bl_label`, because a
literal Chinese label can never be switched back to English.

How F10 gets in here
--------------------
F10 is not Blender's own shortcut -- it belongs to the `use_cn_666` add-on, and
all it really does is flip `preferences.view.use_translate_interface` and
`use_translate_tooltips`, then run its own label-replacement pass:

    def execute(self, context):
        chinese = not context.preferences.view.use_translate_interface
        prefs.view.use_translate_interface = chinese
        prefs.view.use_translate_tooltips = chinese
        custom_terms_runtime.sync_now(chinese=chinese)

So anything registered through `bpy.app.translations` follows F10 for free.
That is the whole mechanism here -- no keymap of our own, no hook into F10.

The same English/Chinese pairs are *also* merged into `use_cn_666`'s
`custom_terms.json` (see `_tools/merge_use_cn_terms.py`), so its replacement
pass agrees with this catalog instead of fighting it. Both directions are
asserted by `_tools/i18n_check.py`.

How to tell whether it worked
-----------------------------
Not from the return values. On this build (5.2.2 LTS) both
`bpy.app.translations.register()` and `unregister()` always return an empty
sequence -- measured, on a catalog that demonstrably works -- so a test that
checks "did register() return a locale?" reports failure for a correct catalog
and would pass for a broken one. The only honest check is a lookup with the
language actually switched:

    view = bpy.context.preferences.view
    view.language = "zh_HANS"          # `zh_CN` is not even in the enum
    view.use_translate_interface = True
    bpy.app.translations.pgettext_iface("UV Constraint", "Operator")
    -> "UV 约束"

Note it takes *both*: with `language` left at `en_US` the msgid comes straight
back, which is correct behaviour and not a failure. `_tools/v13_smoke.py` does
it that way and restores the language afterwards.
"""

import bpy

from .terms_zh import TERMS


def _contexts():
    """Every context a lookup can be made in.

    **There is no catch-all.** Measured on 5.2.2 LTS: a `("*", msgid)` entry
    answers a lookup with context `"*"` (and a bare `pgettext_iface(msgid)`), and
    nothing else -- `pgettext_iface(msgid, "Operator")` comes back untranslated.
    `(None, msgid)` lands in that same no-context bucket, not in a wider one. So
    `"*"` is not a wildcard; it is the *name* of the no-context bucket, and every
    context a string is looked up in has to be registered on its own.

    Blender looks operator labels and tooltips up under `"Operator"`, panel
    headers and tab names under `"Panel"`, and property names and their tooltips
    under `"Property"` -- those three are what this add-on's UI is actually read
    with, and `"*"` covers `pgettext_iface(msgid)` with no context (`use_cn_666`
    checks that one first when deciding whether to defer to us). `"Node"`/
    `"NodeTree"` are kept because that add-on's precedent has them in use, and
    the lowercase names come from `contexts_C_to_py` -- everything Blender's C
    code can ask with.

    Registering the whole set rather than the four that are certainly needed is
    deliberate: which context Blender picks at draw time cannot be observed from
    a background session, and a missing context fails *silently* -- the control
    just stays English. The cost is ~60 extra keys per term, which is nothing
    next to the 17280-entry table `use_cn_666` already carries; the risk is that
    a msgid of ours that Blender itself also translates could override Blender's
    wording in some other panel. `_tools/i18n_check.py` reports every msgid of
    ours that another catalog (`use_cn_666`) translates *differently*, and the
    remaining generic-looking ones are listed there; the ones that overlap are
    all standard terminology where the conventional Chinese is what is used here
    anyway ("Optimize" -> 优化, "Angle Based" -> 角度法).
    """
    names = ["*", None, "Operator", "Panel", "Property", "Node", "NodeTree",
             "RNA"]
    try:
        mapping = bpy.app.translations.contexts_C_to_py
        for value in mapping.values():
            text = str(value)
            if text not in names:
                names.append(text)
    except Exception:
        pass
    return names


def _catalog():
    """`{locale: {(context, msgid): msgstr}}`.

    The key is a **tuple**, not a nested `{context: {msgid: msgstr}}` mapping.
    Blender says so itself if you get it wrong:

        WARNING! In translations' dict of "uv_constraint_kit", en_US language:
            Keys must be tuples of (msgctxt [string or None], msgid [string]),
            this one is not valid, skipping: *

    Note that warning is the *only* feedback you get for a malformed catalog:
    `register()` and `unregister()` both return an empty sequence either way, so
    a broken catalog and a working one look identical from their return values.
    That is why the verification is a lookup with the language switched -- see
    `_tools/v13_smoke.py`.
    """
    contexts = _contexts()
    return {
        "en_US": {},
        "zh_HANS": {(context, msgid): msgstr
                    for msgid, msgstr in TERMS.items()
                    for context in contexts},
    }


def register():
    try:
        bpy.app.translations.unregister(__package__)
    except Exception:
        pass
    bpy.app.translations.register(__package__, _catalog())


def unregister():
    try:
        bpy.app.translations.unregister(__package__)
    except Exception:
        pass


def chinese_now():
    """Is the interface Chinese *right now*?

    Reads the switch F10 flips, not a copy of it, so pressing F10 changes what
    `t()` returns on the very next call.
    """
    try:
        view = bpy.context.preferences.view
    except Exception:
        return False
    if not getattr(view, "use_translate_interface", True):
        return False
    language = getattr(view, "language", "") or ""
    if language.startswith("zh"):
        return True
    try:
        locale = bpy.app.translations.locale or ""
    except Exception:
        locale = ""
    return locale.startswith("zh")


def t(text):
    """Translate a runtime string (report messages, mostly) right now.

    Button labels and tooltips are translated by Blender at draw time and do
    not need this; strings that are *composed* -- `"%s: %d edges" % ...` -- are
    already formatted by the time any UI sees them, so their msgid no longer
    matches a catalog entry and they have to be translated here.
    """
    if not chinese_now():
        return text
    return TERMS.get(text, text)
