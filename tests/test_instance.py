"""The instance file: who the agent is, where its files are, what its briefs say."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shuttle import instance


class InstanceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shuttle-instance-"))
        (self.root / "briefs").mkdir()

    def write(self, doc=None, **extra):
        doc = dict({"agent": {"slug": "kit", "name": "Kit", "tag": "kit"},
                    "owner": {"name": "Robin"}, "secrets": "secrets"}, **(doc or {}), **extra)
        p = self.root / "shuttle.json"
        p.write_text(json.dumps(doc))
        return p

    def brief_file(self, name, text):
        (self.root / "briefs" / f"{name}.md").write_text(text)

    # ------------------------------------------------------------ load

    def test_load_without_the_env_var_names_it(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHUTTLE_INSTANCE", None)
            with self.assertRaises(SystemExit) as cm:
                instance.load()
        self.assertIn("SHUTTLE_INSTANCE is not set", str(cm.exception.code))

    def test_load_from_the_env_var(self):
        p = self.write()
        with mock.patch.dict(os.environ, {"SHUTTLE_INSTANCE": str(p)}):
            inst = instance.load()
        self.assertEqual((inst.path, inst.dir), (p, self.root))
        self.assertEqual(inst.agent_slug, "kit")

    def test_a_missing_file_is_named(self):
        missing = self.root / "nope.json"
        with self.assertRaises(SystemExit) as cm:
            instance.load(missing)
        self.assertIn(str(missing), str(cm.exception.code))

    def test_a_missing_slug_is_refused(self):
        p = self.root / "shuttle.json"
        p.write_text(json.dumps({"agent": {"name": "Kit"}}))
        with self.assertRaises(SystemExit) as cm:
            instance.load(p)
        self.assertIn("agent.slug", str(cm.exception.code))

    def test_a_relative_path_resolves_to_absolute(self):
        self.write()
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            inst = instance.load("shuttle.json")
        finally:
            os.chdir(cwd)
        self.assertTrue(inst.path.is_absolute())
        self.assertEqual(inst.briefs_dir, self.root.resolve() / "briefs")

    def test_unreadable_or_bad_files_exit_naming_the_file(self):
        bad = self.root / "bad.json"
        bad.write_text("{nope")
        for p in (bad, self.root):     # bad JSON; a directory (an OSError)
            with self.subTest(p=p), self.assertRaises(SystemExit) as cm:
                instance.load(p)
            self.assertIn(str(p), str(cm.exception.code))

    def test_a_non_object_agent_names_the_slug(self):
        p = self.root / "shuttle.json"
        p.write_text(json.dumps({"agent": "kit"}))
        with self.assertRaises(SystemExit) as cm:
            instance.load(p)
        self.assertIn("agent.slug", str(cm.exception.code))

    def test_secrets_is_required(self):
        # No setup-specific default (t-1436 review M-4): a missing key names itself.
        for bad in ({}, {"secrets": ""}, {"secret_cmd": ""}):
            p = self.root / "shuttle.json"
            p.write_text(json.dumps({"agent": {"slug": "kit"}, **bad}))
            with self.subTest(doc=bad), self.assertRaises(SystemExit) as cm:
                instance.load(p)
            self.assertIn("secrets", str(cm.exception.code))
            self.assertIn(str(p), str(cm.exception.code))

    def test_read_secret_from_the_secrets_dir(self):
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "tok").write_text("abc\n")
        inst = instance.load(self.write())
        self.assertEqual(inst.read_secret("tok"), "abc\n")
        with self.assertRaises(OSError):
            inst.read_secret("missing")

    def test_secret_cmd_instead_of_a_secrets_dir(self):
        # A store (loom-secrets, a vault CLI) in place of a dir of plaintext files:
        # the command gets the name as its one argument and prints the value.
        cmd = self.root / "get-secret"
        cmd.write_text('#!/bin/sh\n[ "$1" = tok ] || { echo "no $1" >&2; exit 1; }\nprintf abc\n')
        cmd.chmod(0o755)
        doc = {"agent": {"slug": "kit"}, "secret_cmd": "get-secret"}
        p = self.root / "shuttle.json"
        p.write_text(json.dumps(doc))
        inst = instance.load(p)
        self.assertIsNone(inst.secrets)
        self.assertEqual(inst.secret_cmd, cmd)
        self.assertEqual(inst.read_secret("tok"), "abc")
        with self.assertRaises(OSError) as cm:
            inst.read_secret("other")
        self.assertIn("no other", str(cm.exception))
        for bad in ("", "../x", "a/b"):
            with self.subTest(name=bad), self.assertRaises(OSError):
                inst.read_secret(bad)

    def test_a_relative_secrets_resolves_against_the_file(self):
        self.assertEqual(instance.load(self.write()).secrets, self.root / "secrets")

    def test_defaults_derive_from_the_slug(self):
        p = self.root / "shuttle.json"
        p.write_text(json.dumps({"agent": {"slug": "kit"}, "secrets": "/s"}))
        inst = instance.load(p)
        home = Path.home()
        self.assertEqual((inst.agent_name, inst.agent_tag), ("kit", "kit"))
        self.assertEqual(inst.state_root, home / ".local/state")
        self.assertEqual(inst.inbox_dir, home / ".local/state/kit-inbox")
        self.assertEqual(inst.now_dir, home / ".local/state/kit-now")
        self.assertEqual(inst.state_dir("discord"), home / ".local/state/kit-discord")
        self.assertEqual(inst.briefs_dir, self.root / "briefs")
        self.assertEqual(inst.workdir, self.root)
        self.assertEqual((inst.always_deny, inst.guest_deny, inst.channel_notes), ((), (), {}))
        self.assertIsNone(inst.preferred_model)

    def test_paths_expand(self):
        inst = instance.load(self.write(
            workdir="~/kit", state_root="/var/st", inbox_dir="rel/inbox",
            secrets="~/.config/loom/kit/secrets", briefs="prose",
            commands={"claude": "~/.local/bin/claude", "alert": "bin/alert",
                      "send": {"discord": "~/bin/d-send", "slack": "/opt/s-send"}}))
        home = Path.home()
        self.assertEqual(inst.workdir, home / "kit")
        self.assertEqual(inst.state_root, Path("/var/st"))
        self.assertEqual(inst.state_dir("slack"), Path("/var/st/kit-slack"))
        self.assertEqual(inst.inbox_dir, self.root / "rel/inbox")
        self.assertEqual(inst.now_dir, Path("/var/st/kit-now"))
        self.assertEqual(inst.secrets, home / ".config/loom/kit/secrets")
        self.assertEqual(inst.briefs_dir, self.root / "prose")
        self.assertEqual(inst.commands["claude"], home / ".local/bin/claude")
        self.assertEqual(inst.commands["alert"], self.root / "bin/alert")
        self.assertEqual(inst.commands["send"], {"discord": home / "bin/d-send",
                                                 "slack": Path("/opt/s-send")})
        self.assertEqual(inst.expand("x/y"), self.root / "x/y")
        self.assertEqual(inst.expand("~"), home)

    def test_fields(self):
        inst = instance.load(self.write(preferred_model="claude-x", always_deny=["mcp__a"],
                                        guest_deny=["mcp__b", "mcp__c"],
                                        channel_notes={"quiet_channels": "discord-quiet"},
                                        slack={"secrets_env": "s.env"}))
        self.assertEqual((inst.agent_name, inst.agent_tag, inst.owner_name), ("Kit", "kit", "Robin"))
        self.assertEqual(inst.preferred_model, "claude-x")
        self.assertEqual(inst.always_deny, ("mcp__a",))
        self.assertEqual(inst.guest_deny, ("mcp__b", "mcp__c"))
        self.assertEqual(inst.channel_notes, {"quiet_channels": "discord-quiet"})
        self.assertEqual(inst.section("slack"), {"secrets_env": "s.env"})
        self.assertEqual(inst.section("discord"), {})
        self.assertEqual(inst.raw["owner"], {"name": "Robin"})

    # ------------------------------------------------------------ briefs

    def test_brief_substitutes(self):
        self.brief_file("dm", "Hi $name, ${cid}x costs $$5.\n")
        inst = instance.load(self.write())
        self.assertEqual(inst.brief("dm", name="Pat", cid="D1"), "Hi Pat, D1x costs $5.\n")

    def test_brief_returns_the_text_exactly(self):
        self.brief_file("raw", "\n  one\n\n\n")
        inst = instance.load(self.write())
        self.assertEqual(inst.brief("raw"), "\n  one\n\n\n")

    def test_slot_values_are_never_retemplated(self):
        self.brief_file("dm", "new: $new / ctx: $context")
        inst = instance.load(self.write())
        out = inst.brief("dm", new="pay $5 to $context or ${new}", context="$$ $name")
        self.assertEqual(out, "new: pay $5 to $context or ${new} / ctx: $$ $name")

    def test_a_missing_brief_names_the_file(self):
        inst = instance.load(self.write())
        with self.assertRaises(FileNotFoundError) as cm:
            inst.brief("nope")
        self.assertIn("nope.md", str(cm.exception))

    def test_a_missing_slot_names_brief_and_slot(self):
        self.brief_file("dm", "Hi $name and $who")
        inst = instance.load(self.write())
        with self.assertRaises(KeyError) as cm:
            inst.brief("dm", name="Pat")
        self.assertIn("dm", str(cm.exception))
        self.assertIn("who", str(cm.exception))

    def test_check_catches_a_misspelled_slot(self):
        self.brief_file("discord", "In $where: $conext")
        inst = instance.load(self.write())
        errors = inst.check({"discord": ("where", "new", "context")})
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("discord.md: "), errors)
        self.assertIn("conext", errors[0])

    def test_check_catches_a_missing_file(self):
        self.brief_file("discord", "In $where")
        inst = instance.load(self.write())
        errors = inst.check({"discord": ("where",), "discord-guest": ("cards",)})
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("discord-guest.md: "), errors)

    def test_check_catches_a_syntax_error(self):
        self.brief_file("bad", "Hi $ name")
        self.brief_file("also-bad", "Hi ${unclosed")
        self.brief_file("dollar", "costs $5")
        inst = instance.load(self.write())
        errors = inst.check({"bad": ("name",), "also-bad": ("unclosed",), "dollar": ()})
        self.assertEqual(sorted(e.split(":")[0] for e in errors),
                         ["also-bad.md", "bad.md", "dollar.md"], errors)

    def test_check_passes_with_unused_slots_and_ignores_other_briefs(self):
        self.brief_file("discord", "In $where: $$5")
        self.brief_file("slack-dm", "Hi $ broken, but not this bridge's")
        inst = instance.load(self.write())
        self.assertEqual(inst.check({"discord": ("where", "new")}), [])

    def test_check_required_names_need_a_file(self):
        self.brief_file("discord", "In $where")
        inst = instance.load(self.write())
        self.assertEqual(inst.check({"discord": ("where",)}, required=("discord",)), [])
        errors = inst.check({"discord": ("where",)}, required=("discord-guest",))
        self.assertEqual(len(errors), 1)
        self.assertIn("discord-guest.md", errors[0])

    def test_check_with_no_briefs_dir(self):
        inst = instance.load(self.write(briefs="absent"))
        self.assertEqual(inst.check({}), [])
        self.assertEqual(len(inst.check({"dm": ()})), 1)

    def test_check_uses_requires_a_brief_to_use_a_slot(self):
        self.brief_file("discord", "In $where, no notes")
        self.brief_file("discord-guest", "floor only, no cards: $send")
        inst = instance.load(self.write())
        slots = {"discord": ("where", "notes"), "discord-guest": ("cards", "send")}
        errors = inst.check(slots, uses={"discord": ("notes",), "discord-guest": ("cards",)})
        self.assertEqual(sorted(errors), ["discord-guest.md: never uses slot 'cards'",
                                          "discord.md: never uses slot 'notes'"])
        self.brief_file("discord", "In $where${notes}!")
        self.brief_file("discord-guest", "[$cards] $send")
        inst = instance.load(self.write())
        self.assertEqual(inst.check(slots, uses={"discord": ("notes",), "discord-guest": ("cards",)}), [])

    def test_check_ignores_a_readme_in_the_briefs_dir(self):
        self.brief_file("discord", "In $where")
        self.brief_file("README", "These files are exact bytes. $ not a slot ${")
        inst = instance.load(self.write())
        self.assertEqual(inst.check({"discord": ("where",)}, required=("discord",)), [])

    def test_brief_texts_are_read_once(self):
        """Loaded at check(): a file edited or deleted later can't break a render."""
        self.brief_file("discord", "In $where")
        inst = instance.load(self.write())
        self.assertEqual(inst.check({"discord": ("where",)}), [])
        self.brief_file("discord", "In $conext")
        self.assertEqual(inst.brief("discord", where="#x"), "In #x")
        (self.root / "briefs" / "discord.md").unlink()
        self.assertEqual(inst.brief("discord", where="#y"), "In #y")
        self.assertEqual(instance.load(self.write()).check({"discord": ("where",)}),
                         [f"discord.md: no such brief in {self.root / 'briefs'}"])

    def test_brief_names_itself_on_a_bad_placeholder(self):
        self.brief_file("dm", "costs $5")
        inst = instance.load(self.write())
        with self.assertRaises(ValueError) as cm:
            inst.brief("dm")
        self.assertIn("dm", str(cm.exception))

    # ------------------------------------------------------------ ext

    def test_no_ext_gives_no_op_hooks(self):
        e = instance.load(self.write()).ext()
        self.assertIsNone(e.tick(object(), {}))
        self.assertEqual(e.dm_extra(object(), {}, 2), {"text": "", "env": None, "on_claimed": None})
        self.assertEqual(e.check(object()), [])

    def test_ext_hooks_load_once_and_missing_ones_are_no_ops(self):
        (self.root / "ext").mkdir()
        (self.root / "ext" / "kit.py").write_text(
            "LOADS = []\nLOADS.append(1)\n"
            "def tick(bridge, state):\n    state['ticked'] = True\n")
        inst = instance.load(self.write(ext="ext/kit.py"))
        e = inst.ext()
        self.assertIs(inst.ext(), e)
        self.assertEqual(e.module.LOADS, [1])
        state = {}
        e.tick(None, state)
        self.assertEqual(state, {"ticked": True})
        self.assertEqual(e.dm_extra(None, {}, 2), {"text": "", "env": None, "on_claimed": None})
        self.assertEqual(e.check(inst), [])

    def test_ext_check_gets_the_instance(self):
        (self.root / "ext").mkdir()
        (self.root / "ext" / "kit.py").write_text(
            "def check(instance):\n    return [f'{instance.agent_slug}: bad']\n")
        inst = instance.load(self.write(ext="ext/kit.py"))
        self.assertEqual(inst.ext().check(inst), ["kit: bad"])

    def test_a_missing_ext_file_fails_loudly(self):
        inst = instance.load(self.write(ext="ext/none.py"))
        with self.assertRaises(SystemExit) as cm:
            inst.ext()
        self.assertIn("none.py", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
