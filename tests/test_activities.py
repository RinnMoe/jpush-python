from datetime import datetime,timedelta,timezone
import json
import pytest
from jpush.activities import ActivityHint,VerifiedActivity,DeadlineBook,decode_activity

NOW=datetime(2030,1,1,tzinfo=timezone.utc)
def item(**changes):return VerifiedActivity(**{'course_id':42,'activity_id':73,'kind':'homework','title':'Synthetic homework','due_at':NOW+timedelta(days=2),**changes})

def test_activity_hint_ignores_untrusted_deadline_and_rejects_conflicts():
    raw={'n_extras':json.dumps({'acme-course-id':'42','acme-activity-id':'73','due_at':'bogus','token':'synthetic'})}
    assert decode_activity(json.dumps(raw))==ActivityHint(42,73)
    assert decode_activity(json.dumps({**raw,'course_id':43})) is None
    for bad in [True,0,'73x',2**63]:assert decode_activity(json.dumps({'course_id':42,'activity_id':bad})) is None
    assert decode_activity('a'*4097) is None

def test_new_and_changed_deadlines_replace_local_timer():
    book=DeadlineBook();old=item();assert book.update(old);assert not book.update(old);assert not book.due(NOW)
    updated=item(due_at=NOW+timedelta(hours=2));assert book.update(updated)
    assert book.due(NOW)==[(updated,'due',updated.due_at)]
    book.mark(updated,'due',updated.due_at);assert not book.due(NOW)
    changed=item(due_at=NOW+timedelta(hours=3));book.update(changed);assert len(book.due(NOW))==1
    with pytest.raises(ValueError):book.mark(updated,'due',updated.due_at)
    book.remove(42,73);assert not book.due(NOW)

def test_expired_naive_and_over_capacity_are_not_scheduled():
    with pytest.raises(ValueError):item(due_at=NOW.replace(tzinfo=None))
    book=DeadlineBook(limit=1);book.update(item())
    with pytest.raises(ValueError):book.update(item(activity_id=74))
    book.update(item(due_at=NOW-timedelta(seconds=1)));assert not book.due(NOW);book.prune(NOW)
    book.update(item(activity_id=74))
    with pytest.raises(ValueError):book.due(NOW.replace(tzinfo=None))

def test_exam_start_and_delivery_retry_stay_separate():
    book=DeadlineBook();exam=item(kind='exam',starts_at=NOW+timedelta(minutes=10),due_at=NOW+timedelta(hours=1));book.update(exam)
    assert len(book.due(NOW))==2
    book.mark(exam,'start',exam.starts_at);assert book.due(NOW)==[(exam,'due',exam.due_at)]
    other=DeadlineBook();other.update(exam);assert len(other.due(NOW))==2  # independent account
