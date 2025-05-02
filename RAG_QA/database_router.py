class SalesDatabaseRouter:
    """
    A database router to route queries for the 'sales' app to 'sales_db',
    while keeping all other apps in the default database.
    """

    def db_for_read(self, model, **hints):
        """Send read operations for 'sales' models to 'sales_db'."""
        if model._meta.app_label == "sales":
            return "sales_db"
        return "default"

    def db_for_write(self, model, **hints):
        """Send write operations for 'sales' models to 'sales_db'."""
        if model._meta.app_label == "sales":
            return "sales_db"
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        """
        Allow relations only within the same database.
        """
        if obj1._state.db == obj2._state.db:
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Ensure 'sales' models only migrate on 'sales_db',
        and other models only migrate on 'default'.
        """
        if app_label == "sales":
            return db == "sales_db"
        return db == "default"
