"""Hardware GUI authorization must precede server startup or any BLE access."""
import io
import sys
import pytest
from kickrcoach import web


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_gui_control_flag_refused_without_hardware(monkeypatch):
    monkeypatch.setattr(sys,'argv',['kickrcoach-web','--enable-control'])
    with pytest.raises(SystemExit) as exc:
        web.main()
    assert exc.value.code==2


@pytest.mark.parametrize('reply', ['no', '', EOFError()])
def test_cli_cancellation_never_starts_server_or_bluetooth(monkeypatch,reply,tmp_path):
    import uvicorn
    config=tmp_path/'config.toml'
    config.write_text('trainer_id="trainer-fixture"\nhr_id="hr-fixture"\n')
    monkeypatch.setattr(sys,'argv',['kickrcoach-web','--hardware','--enable-control','--config',str(config)])
    monkeypatch.setattr(sys,'stdin',Terminal())
    monkeypatch.setattr(sys,'stdout',Terminal())
    def answer(_prompt):
        if isinstance(reply,Exception):
            raise reply
        return reply
    monkeypatch.setattr('builtins.input',answer)
    monkeypatch.setattr(uvicorn,'run',lambda *_a,**_k:pytest.fail('server started without confirmation'))
    with pytest.raises(SystemExit) as exc:
        web.main()
    assert exc.value.code==2


def test_noninteractive_hardware_control_is_refused(monkeypatch,tmp_path):
    config=tmp_path/'config.toml'
    config.write_text('trainer_id="trainer-fixture"\nhr_id="hr-fixture"\n')
    monkeypatch.setattr(sys,'argv',['kickrcoach-web','--hardware','--enable-control','--config',str(config)])
    monkeypatch.setattr(sys,'stdin',io.StringIO())
    with pytest.raises(SystemExit) as exc:
        web.main()
    assert exc.value.code==2


def test_exact_confirmation_enables_control_only_for_this_server(monkeypatch,tmp_path):
    import uvicorn
    config=tmp_path/'config.toml'
    config.write_text('trainer_id="trainer-fixture"\nhr_id="hr-fixture"\n')
    monkeypatch.setattr(sys,'argv',['kickrcoach-web','--hardware','--enable-control','--config',str(config)])
    monkeypatch.setattr(sys,'stdin',Terminal())
    monkeypatch.setattr(sys,'stdout',Terminal())
    prompts=[]
    def answer(prompt):
        prompts.append(prompt)
        return 'CONTROL 50 W'
    monkeypatch.setattr('builtins.input',answer)
    calls=[]
    original=web.create_app
    def create(config,**kwargs):
        calls.append(kwargs)
        return original(config,**kwargs)
    monkeypatch.setattr(web,'create_app',create)
    monkeypatch.setattr(uvicorn,'run',lambda *_a,**_k:None)
    web.main()
    assert calls==[{'enable_control':True}]
    assert 'CONTROL 50 W' in prompts[0]
