from sqlalchemy import Integer, literal_column


def history_order(db, model) -> tuple:
    # Checkpoint counts and delivery cursors describe append order in the local SQLite store.
    if db.get_bind().dialect.name == 'sqlite':
        return (literal_column(f'{model.__tablename__}.rowid', Integer()),)
    return model.created_at, model.id
