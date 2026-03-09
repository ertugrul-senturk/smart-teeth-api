from bson import ObjectId
from datetime import datetime
import base64


class SyncRepository:

    def __init__(self, db):
        self.db = db

    def find_existing_ids(self, user_id, collection_key, object_ids):
        collection = self.db[collection_key]
        valid_object_ids = []
        for oid in object_ids:
            try:
                valid_object_ids.append(ObjectId(oid))
            except:
                valid_object_ids.append(oid)

        query = {
            'userId': user_id,
            'id': {'$in': valid_object_ids},
            '$or': [
                {'isDeleted': {'$exists': False}},
                {'isDeleted': False}
            ]
        }

        existing_docs = collection.find(query, {'id': 1})
        return {str(doc['id']) for doc in existing_docs}

    def find_missing_data(self, user_id, collection_key, existing_ids_on_client):
        collection = self.db[collection_key]

        client_object_ids = []
        for oid in existing_ids_on_client:
            try:
                client_object_ids.append(ObjectId(oid))
            except:
                client_object_ids.append(oid)

        query = {
            'userId': user_id,
            'id': {'$nin': client_object_ids},
            '$or': [
                {'isDeleted': {'$exists': False}},
                {'isDeleted': False}
            ]
        }

        docs = list(collection.find(query))

        for doc in docs:
            doc['_id'] = str(doc['_id'])
            if 'userId' in doc:
                doc['userId'] = str(doc['userId'])

        return docs

    def bulk_insert(self, user_id, collection_key, documents):
        collection = self.db[collection_key]

        if not documents:
            return []

        inserted_ids = []
        for doc in documents:
            doc['userId'] = user_id

            if '_id' in doc and doc['_id']:
                try:
                    doc['_id'] = ObjectId(doc['_id'])
                except:
                    pass

            # Add timestamps
            doc['createdAt'] = doc.get('createdAt', datetime.utcnow())
            doc['updatedAt'] = datetime.utcnow()
            doc['isDeleted'] = False

            try:
                result = collection.insert_one(doc)
                inserted_ids.append(str(result.inserted_id))
            except Exception as e:
                if 'duplicate key' in str(e).lower():
                    doc_id = doc.pop('_id', None)
                    if doc_id:
                        collection.update_one(
                            {'_id': doc_id, 'userId': user_id},
                            {'$set': doc}
                        )
                        inserted_ids.append(str(doc_id))
                else:
                    raise e

        return inserted_ids

    def upsert_document(self, user_id, collection_key, document):
        collection = self.db[collection_key]

        document['userId'] = user_id
        document['updatedAt'] = datetime.utcnow()
        document['isDeleted'] = False

        doc_id = document.pop('_id', None)

        if doc_id:
            try:
                obj_id = ObjectId(doc_id)
            except:
                obj_id = doc_id

            collection.update_one(
                {'_id': obj_id, 'userId': user_id},
                {'$set': document, '$setOnInsert': {'createdAt': datetime.utcnow()}},
                upsert=True
            )
            return str(obj_id)
        else:
            document['createdAt'] = datetime.utcnow()
            result = collection.insert_one(document)
            return str(result.inserted_id)

    def mark_as_deleted(self, user_id, collection_key, object_ids):
        collection = self.db[collection_key]

        valid_object_ids = []
        for oid in object_ids:
            try:
                valid_object_ids.append(ObjectId(oid))
            except:
                valid_object_ids.append(oid)

        result = collection.update_many(
            {
                'id': {'$in': valid_object_ids},
                'userId': user_id
            },
            {
                '$set': {
                    'isDeleted': True,
                    'deletedAt': datetime.utcnow(),
                    'updatedAt': datetime.utcnow()
                }
            }
        )

        return result.modified_count


class ImageRepository:
    COLLECTION_NAME = 'images'

    def __init__(self, db):
        self.db = db
        self.collection = db[self.COLLECTION_NAME]

    def store_image(self, user_id, image_data):
        if not image_data:
            return None
        doc = {
            'userId': user_id,
            'base64Data': image_data.get('base64', ''),
            'mimeType': image_data.get('mimeType', 'image/jpeg'),
            'createdAt': datetime.utcnow()
        }
        result = self.collection.insert_one(doc)
        return str(result.inserted_id)

    def store_images_binary(self, user_id, images_binary):
        if not images_binary:
            return []

        image_ids = []
        for img in images_binary:
            base64_data = base64.b64encode(img['data']).decode('utf-8')

            doc = {
                'userId': user_id,
                'filename': img.get('filename', ''),
                'base64Data': base64_data,
                'mimeType': img.get('mimeType', 'image/jpeg'),
                'size': len(img['data']),
                'createdAt': datetime.utcnow()
            }

            result = self.collection.insert_one(doc)
            image_ids.append(str(result.inserted_id))

        return image_ids

    def get_images_by_ids(self, user_id, image_ids):
        if not image_ids:
            return []

        object_ids = []
        for img_id in image_ids:
            try:
                object_ids.append(ObjectId(img_id))
            except:
                object_ids.append(img_id)

        query = {
            'userId': user_id,
            '_id': {'$in': object_ids}
        }

        docs = list(self.collection.find(query))

        result = []
        for doc in docs:
            result.append({
                '_id': str(doc['_id']),
                'base64': doc.get('base64Data', ''),
                'mimeType': doc.get('mimeType', 'image/jpeg'),
                'filename': doc.get('filename', ''),
                'size': doc.get('size', 0),
                'createdAt': doc.get('createdAt', '').isoformat() if doc.get('createdAt') else None
            })

        return result
