"""GUI startup uses the explicit flag, not an interactive phrase/TTY."""
import io
import sys
import pytest
from kickrcoach import web


def test_gui_control_flag_refused_without_hardware(monkeypatch):
    monkeypatch.setattr(sys,'argv',['kickrcoach-web','--enable-control'])
    with pytest.raises(SystemExit) as exc:
        web.main()
    assert exc.value.code==2


@pytest.mark.parametrize('enabled',[False,True])
def test_noninteractive_startup_never_reads_input_and_preserves_authorization(monkeypatch,tmp_path,enabled):
    import uvicorn
    config=tmp_path/'config.toml'
    config.write_text('trainer_id="trainer-fixture"\nhr_id="hr-fixture"\n')
    args=['kickrcoach-web','--hardware','--config',str(config)]
    if enabled:
        args.append('--enable-control')
    monkeypatch.setattr(sys,'argv',args)
    monkeypatch.setattr(sys,'stdin',io.StringIO())
    monkeypatch.setattr(sys,'stdout',io.StringIO())
    monkeypatch.setattr('builtins.input',lambda *_a:pytest.fail('unexpected startup prompt'))
    app_calls=[]
    original=web.create_app
    def create(config,**kwargs):
        app_calls.append(kwargs)
        return original(config,**kwargs)
    monkeypatch.setattr(web,'create_app',create)
    server_calls=[]
    monkeypatch.setattr(uvicorn,'run',lambda *_a,**kwargs:server_calls.append(kwargs))
    web.main()
    assert app_calls==[{'enable_control':enabled}]
    assert len(server_calls)==1 and server_calls[0]['host']=='127.0.0.1'
    if enabled:
        assert 'Trainingsgrenze 300 W' in sys.stdout.getvalue()
