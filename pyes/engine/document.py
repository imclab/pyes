#!/usr/bin/env python
# -*- coding: utf-8 -*-

import copy
from . import signals
from base import (DocumentMetaclass, TopLevelDocumentMetaclass, BaseDocument,
                  BaseDict, BaseList)
from queryset import OperationError
from connection import get_db, DEFAULT_CONNECTION_NAME

__all__ = ['Document', 'EmbeddedDocument', 'DynamicDocument',
           'DynamicEmbeddedDocument', 'OperationError', 'InvalidCollectionError']


class InvalidCollectionError(Exception):
    pass


class EmbeddedDocument(BaseDocument):
    """A :class:`~esengine.Document` that isn't stored in its own
    collection.  :class:`~esengine.EmbeddedDocument`\ s should be used as
    fields on :class:`~esengine.Document`\ s through the
    :class:`~esengine.EmbeddedDocumentField` field type.
    """

    __metaclass__ = DocumentMetaclass

    def __init__(self, *args, **kwargs):
        super(EmbeddedDocument, self).__init__(*args, **kwargs)
        self._changed_fields = []

    def __delattr__(self, *args, **kwargs):
        """Handle deletions of fields"""
        field_name = args[0]
        if field_name in self._fields:
            default = self._fields[field_name].default
            if callable(default):
                default = default()
            setattr(self, field_name, default)
        else:
            super(EmbeddedDocument, self).__delattr__(*args, **kwargs)


class Document(BaseDocument):
    """The base class used for defining the structure and properties of
    collections of documents stored in ElasticSearch. Inherit from this class, and
    add fields as class attributes to define a document's structure.
    Individual documents may then be created by making instances of the
    :class:`~esengine.Document` subclass.

    By default, the ElasticSearch collection used to store documents created using a
    :class:`~esengine.Document` subclass will be the name of the subclass
    converted to lowercase. A different collection may be specified by
    providing :attr:`collection` to the :attr:`meta` dictionary in the class
    definition.

    A :class:`~esengine.Document` subclass may be itself subclassed, to
    create a specialised version of the document that will be stored in the
    same collection. To facilitate this behaviour, `_cls` and `_types`
    fields are added to documents (hidden though the ESEngine interface
    though). To disable this behaviour and remove the dependence on the
    presence of `_cls` and `_types`, set :attr:`allow_inheritance` to
    ``False`` in the :attr:`meta` dictionary.

    A :class:`~esengine.Document` may use a **Capped Collection** by
    specifying :attr:`max_documents` and :attr:`max_size` in the :attr:`meta`
    dictionary. :attr:`max_documents` is the maximum number of documents that
    is allowed to be stored in the collection, and :attr:`max_size` is the
    maximum size of the collection in bytes. If :attr:`max_size` is not
    specified and :attr:`max_documents` is, :attr:`max_size` defaults to
    10000000 bytes (10MB).

    Indexes may be created by specifying :attr:`indexes` in the :attr:`meta`
    dictionary. The value should be a list of field names or tuples of field
    names. Index direction may be specified by prefixing the field names with
    a **+** or **-** sign.

    By default, _types will be added to the start of every index (that
    doesn't contain a list) if allow_inheritence is True. This can be
    disabled by either setting types to False on the specific index or
    by setting index_types to False on the meta dictionary for the document.
    """
    __metaclass__ = TopLevelDocumentMetaclass

    @apply
    def pk():
        """Primary key alias
        """
        def fget(self):
            return getattr(self, self._meta['id_field'])
        def fset(self, value):
            return setattr(self, self._meta['id_field'], value)
        return property(fget, fset)

    @classmethod
    def _get_db(cls):
        """Some Model using other db_alias"""
        return get_db(cls._meta.get("db_alias", DEFAULT_CONNECTION_NAME ))

    def save(self, safe=True, force_insert=False, validate=True, bulk=False,
            cascade=None, cascade_kwargs=None, _refs=None):
        """Save the :class:`~esengine.Document` to the database. If the
        document already exists, it will be updated, otherwise it will be
        created.

        If ``safe=True`` and the operation is unsuccessful, an
        :class:`~esengine.OperationError` will be raised.

        :param safe: check if the operation succeeded before returning
        :param force_insert: only try to create a new document, don't allow
            updates of existing documents
        :param validate: validates the document; set to ``False`` to skip.
        :param cascade: Sets the flag for cascading saves.  You can set a default by setting
            "cascade" in the document __meta__
        :param cascade_kwargs: optional kwargs dictionary to be passed throw to cascading saves
        :param _refs: A list of processed references used in cascading saves

        """
        signals.pre_save.send(self.__class__, document=self)

        if validate:
            self.validate()
        es = self._get_db()
        doc = self.to_es()
        doc._meta.connection =es
        doc._meta.index =doc._meta.connection.default_indices[0]
        #doc.save(force_insert=force_insert, bulk=bulk)
        doc.save(bulk=bulk)
        if not bulk:
            es.dirty = True
            id_field = self._meta['id_field']
            self[id_field] = self._fields[id_field].to_python(doc._meta.id)

            self._changed_fields = []
            self._created = False
            signals.post_save.send(self.__class__, document=self, created=doc._meta.version==1)
        #TODO: add signals in bulk create

    def cascade_save(self, *args, **kwargs):
        """Recursively saves any references / generic references on an object"""
        from fields import ReferenceField, GenericReferenceField
        _refs = kwargs.get('_refs', []) or []
        for name, cls in self._fields.items():
            if not isinstance(cls, (ReferenceField, GenericReferenceField)):
                continue
            ref = getattr(self, name)
            if not ref:
                continue
            ref_id = "%s,%s" % (ref.__class__.__name__, str(ref._data))
            if ref and ref_id not in _refs:
                _refs.append(ref_id)
                kwargs["_refs"] = _refs
                ref.save(**kwargs)
                ref._changed_fields = []

    def update(self, **kwargs):
        """Performs an update on the :class:`~esengine.Document`
        A convenience wrapper to :meth:`~esengine.QuerySet.update`.

        Raises :class:`OperationError` if called on an object that has not yet
        been saved.
        """
        if not self.pk:
            raise OperationError('attempt to update a document not yet saved')

        # Need to add shard key to query, or you get an error
        select_dict = {'pk': self.pk}
        shard_key = self.__class__._meta.get('shard_key', tuple())
        for k in shard_key:
            select_dict[k] = getattr(self, k)
        return self.__class__.objects(**select_dict).update_one(**kwargs)

    def delete(self, safe=False):
        """Delete the :class:`~esengine.Document` from the database. This
        will only take effect if the document has been previously saved.

        :param safe: check if the operation succeeded before returning
        """
        signals.pre_delete.send(self.__class__, document=self)

        try:
            self.__class__.objects(pk=self.pk).delete(safe=safe)
        except pymongo.errors.OperationFailure, err:
            message = u'Could not delete document (%s)' % err.message
            raise OperationError(message)

        signals.post_delete.send(self.__class__, document=self)

    def select_related(self, max_depth=1):
        """Handles dereferencing of :class:`~bson.dbref.DBRef` objects to
        a maximum depth in order to cut down the number queries to mongodb.

        .. versionadded:: 0.5
        """
        from dereference import DeReference
        self._data = DeReference()(self._data, max_depth)
        return self

    def reload(self, max_depth=1):
        """Reloads all attributes from the database.

        .. versionadded:: 0.1.2
        .. versionchanged:: 0.6  Now chainable
        """
        id_field = self._meta['id_field']
        obj = self.__class__.objects(
                **{id_field: self[id_field]}
              ).first().select_related(max_depth=max_depth)
        for field in self._fields:
            setattr(self, field, self._reload(field, obj[field]))
        if self._dynamic:
            for name in self._dynamic_fields.keys():
                setattr(self, name, self._reload(name, obj._data[name]))
        self._changed_fields = obj._changed_fields
        return obj

    def _reload(self, key, value):
        """Used by :meth:`~esengine.Document.reload` to ensure the
        correct instance is linked to self.
        """
        if isinstance(value, BaseDict):
            value = [(k, self._reload(k, v)) for k, v in value.items()]
            value = BaseDict(value, self, key)
        elif isinstance(value, BaseList):
            value = [self._reload(key, v) for v in value]
            value = BaseList(value, self, key)
        elif isinstance(value, (EmbeddedDocument, DynamicEmbeddedDocument)):
            value._changed_fields = []
        return value

    def to_dbref(self):
        """Returns an instance of :class:`~bson.dbref.DBRef` useful in
        `__raw__` queries."""
        if not self.pk:
            msg = "Only saved documents can have a valid dbref"
            raise OperationError(msg)
        return DBRef(self.__class__._get_collection_name(), self.pk)

    @classmethod
    def register_delete_rule(cls, document_cls, field_name, rule):
        """This method registers the delete rules to apply when removing this
        object.
        """
        cls._meta['delete_rules'][(document_cls, field_name)] = rule

    @classmethod
    def drop_collection(cls):
        """Drops the entire collection associated with this
        :class:`~esengine.Document` type from the database.
        """
        from esengine.queryset import QuerySet
        db = cls._get_db()
        db.delete_mapping(db._default_indices[0], cls._get_collection_name())
        QuerySet._reset_already_indexed(cls)


class DynamicDocument(Document):
    """A Dynamic Document class allowing flexible, expandable and uncontrolled
    schemas.  As a :class:`~esengine.Document` subclass, acts in the same
    way as an ordinary document but has expando style properties.  Any data
    passed or set against the :class:`~esengine.DynamicDocument` that is
    not a field is automatically converted into a
    :class:`~esengine.BaseDynamicField` and data can be attributed to that
    field.

    ..note::

        There is one caveat on Dynamic Documents: fields cannot start with `_`
    """
    __metaclass__ = TopLevelDocumentMetaclass
    _dynamic = True

    def __delattr__(self, *args, **kwargs):
        """Deletes the attribute by setting to None and allowing _delta to unset
        it"""
        field_name = args[0]
        if field_name in self._dynamic_fields:
            setattr(self, field_name, None)
        else:
            super(DynamicDocument, self).__delattr__(*args, **kwargs)


class DynamicEmbeddedDocument(EmbeddedDocument):
    """A Dynamic Embedded Document class allowing flexible, expandable and
    uncontrolled schemas. See :class:`~esengine.DynamicDocument` for more
    information about dynamic documents.
    """

    __metaclass__ = DocumentMetaclass
    _dynamic = True

    def __delattr__(self, *args, **kwargs):
        """Deletes the attribute by setting to None and allowing _delta to unset
        it"""
        field_name = args[0]
        setattr(self, field_name, None)

