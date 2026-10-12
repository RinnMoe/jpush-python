import json
from jpush.rollcall import decode_rollcall

def test_rollcall_literal_types_and_android_extras():
    for event,kind in [('NUMBER_ROLLCALL','number'),('RADAR_ROLLCALL','radar'),('QRCODE_ROLLCALL','qr')]:
        hint=decode_rollcall(json.dumps({'n_title':'Human title','n_extras':json.dumps({'event_type':event,'acme-course-id':'42','acme-rollcall-id':'73'})}))
        assert (hint.kind,hint.course_id,hint.rollcall_id)==(kind,42,73)

def test_rollcall_conflicting_spoofed_and_incomplete_fields():
    good={'event_type':'RADAR_ROLLCALL','course_id':42,'rollcall_id':73}
    assert decode_rollcall(json.dumps({**good,'title':'Human notification'}))
    assert decode_rollcall(json.dumps({**good,'extras':{'course_id':43}})) is None
    for key,value in [('course_id',False),('rollcall_id',0),('course_id',-1),('rollcall_id','73junk'),('rollcall_id',2**63),('event_type','UNKNOWN')]:
        assert decode_rollcall(json.dumps({**good,key:value})) is None
    for content in ['', '[]','null','{','a'*4097,json.dumps({'course_id':42,'rollcall_id':73})]:
        assert decode_rollcall(content) is None

def test_rollcall_decoding_does_not_return_tokens_or_notification_body():
    hint=decode_rollcall(json.dumps({'event_type':'NUMBER_ROLLCALL','course_id':42,'rollcall_id':73,'access_token':'synthetic-only','body':'not forwarded'}))
    assert 'synthetic-only' not in repr(hint)
    assert 'body' not in repr(hint)
