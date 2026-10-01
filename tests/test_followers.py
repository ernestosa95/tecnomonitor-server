"""Los usuarios desactivados no se mandan a Asana como followers."""
import database
from alerts_engine.config import _followers_de


def test_followers_omite_desactivados_y_sin_asana(db):
    U = database.UserModel
    db.add_all([U(email="a@x", hashed_password="x", asana_id="111", is_active=True),
                U(email="b@x", hashed_password="x", asana_id="222", is_active=False),
                U(email="c@x", hashed_password="x", asana_id="333"),
                U(email="d@x", hashed_password="x", asana_id=None, is_active=True)])
    db.commit()
    assert sorted(_followers_de(db, {"global_alert_responsible_email": "a@x, b@x,c@x,d@x"})) == ["111", "333"]
    assert _followers_de(db, {"global_alert_responsible_email": "b@x"}) == []
