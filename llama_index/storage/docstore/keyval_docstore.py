"""Document store."""

from typing import Dict, List, Optional, Sequence, Tuple

from llama_index.schema import BaseNode, TextNode
from llama_index.storage.docstore.types import (
    BaseDocumentStore,
    RefDocInfo,
)
from llama_index.storage.docstore.utils import doc_to_json, json_to_doc
from llama_index.storage.kvstore.types import DEFAULT_BATCH_SIZE, BaseKVStore

DEFAULT_NAMESPACE = "docstore"


class KVDocumentStore(BaseDocumentStore):
    """Document (Node) store.

    NOTE: at the moment, this store is primarily used to store Node objects.
    Each node will be assigned an ID.

    The same docstore can be reused across index structures. This
    allows you to reuse the same storage for multiple index structures;
    otherwise, each index would create a docstore under the hood.

    .. code-block:: python
        nodes = SentenceSplitter().get_nodes_from_documents()
        docstore = SimpleDocumentStore()
        docstore.add_documents(nodes)
        storage_context = StorageContext.from_defaults(docstore=docstore)

        summary_index = SummaryIndex(nodes, storage_context=storage_context)
        vector_index = VectorStoreIndex(nodes, storage_context=storage_context)
        keyword_table_index = SimpleKeywordTableIndex(nodes, storage_context=storage_context)

    This will use the same docstore for multiple index structures.

    Args:
        kvstore (BaseKVStore): key-value store
        namespace (str): namespace for the docstore

    """

    def __init__(
        self,
        kvstore: BaseKVStore,
        namespace: Optional[str] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        """Init a KVDocumentStore."""
        self._kvstore = kvstore
        self._namespace = namespace or DEFAULT_NAMESPACE
        self._node_collection = f"{self._namespace}/data"
        self._ref_doc_collection = f"{self._namespace}/ref_doc_info"
        self._metadata_collection = f"{self._namespace}/metadata"
        self._batch_size = batch_size

    @property
    def docs(self) -> Dict[str, BaseNode]:
        """Get all documents.

        Returns:
            Dict[str, BaseDocument]: documents

        """
        json_dict = self._kvstore.get_all(collection=self._node_collection)
        return {key: json_to_doc(json) for key, json in json_dict.items()}

    def _get_kv_pairs_for_insert(
        self, node: BaseNode, ref_doc_info: Optional[RefDocInfo], store_text: bool
    ) -> Tuple[
        Optional[Tuple[str, dict]],
        Optional[Tuple[str, dict]],
        Optional[Tuple[str, dict]],
    ]:
        node_kv_pair = None
        metadata_kv_pair = None
        ref_doc_kv_pair = None

        node_key = node.node_id
        data = doc_to_json(node)
        if store_text:
            node_kv_pair = (node_key, data)

        # update doc_collection if needed
        metadata = {"doc_hash": node.hash}
        if ref_doc_info is not None and node.ref_doc_id:
            if node.node_id not in ref_doc_info.node_ids:
                ref_doc_info.node_ids.append(node.node_id)
            if not ref_doc_info.metadata:
                ref_doc_info.metadata = node.metadata or {}

            # update metadata with map
            metadata["ref_doc_id"] = node.ref_doc_id

            metadata_kv_pair = (node_key, metadata)
            ref_doc_kv_pair = (node.ref_doc_id, ref_doc_info.to_dict())
        else:
            metadata_kv_pair = (node_key, metadata)

        return node_kv_pair, metadata_kv_pair, ref_doc_kv_pair

    def _merge_ref_doc_kv_pairs(self, ref_doc_kv_pairs: dict) -> List[Tuple[str, dict]]:
        merged_ref_doc_kv_pairs = []
        for key, kv_pairs in ref_doc_kv_pairs.items():
            merged_node_ids = []
            metadata = {}
            for kv_pair in kv_pairs:
                merged_node_ids.extend(kv_pair[1].get("node_ids", []))
                metadata.update(kv_pair[1].get("metadata", {}))
            merged_ref_doc_kv_pairs.append(
                (key, {"node_ids": merged_node_ids, "metadata": metadata})
            )

        return merged_ref_doc_kv_pairs

    def add_documents(
        self,
        nodes: Sequence[BaseNode],
        allow_update: bool = True,
        batch_size: Optional[int] = None,
        store_text: bool = True,
    ) -> None:
        """Add a document to the store.

        Args:
            docs (List[BaseDocument]): documents
            allow_update (bool): allow update of docstore from document

        """
        batch_size = batch_size or self._batch_size

        node_kv_pairs = []
        metadata_kv_pairs = []
        ref_doc_kv_pairs: Dict[str, List[Tuple[str, dict]]] = {}

        for node in nodes:
            # NOTE: doc could already exist in the store, but we overwrite it
            if not allow_update and self.document_exists(node.node_id):
                raise ValueError(
                    f"node_id {node.node_id} already exists. "
                    "Set allow_update to True to overwrite."
                )
            ref_doc_info = None
            if isinstance(node, TextNode) and node.ref_doc_id is not None:
                ref_doc_info = self.get_ref_doc_info(node.ref_doc_id) or RefDocInfo()

            (
                node_kv_pair,
                metadata_kv_pair,
                ref_doc_kv_pair,
            ) = self._get_kv_pairs_for_insert(node, ref_doc_info, store_text)

            if node_kv_pair is not None:
                node_kv_pairs.append(node_kv_pair)
            if metadata_kv_pair is not None:
                metadata_kv_pairs.append(metadata_kv_pair)
            if ref_doc_kv_pair is not None:
                key = ref_doc_kv_pair[0]
                if key not in ref_doc_kv_pairs:
                    ref_doc_kv_pairs[key] = []
                ref_doc_kv_pairs[key].append(ref_doc_kv_pair)

        self._kvstore.put_all(
            node_kv_pairs,
            collection=self._node_collection,
            batch_size=batch_size,
        )
        self._kvstore.put_all(
            metadata_kv_pairs,
            collection=self._metadata_collection,
            batch_size=batch_size,
        )

        # multiple nodes can point to the same ref_doc_id
        merged_ref_doc_kv_pairs = self._merge_ref_doc_kv_pairs(ref_doc_kv_pairs)
        self._kvstore.put_all(
            merged_ref_doc_kv_pairs,
            collection=self._ref_doc_collection,
            batch_size=batch_size,
        )

    async def async_add_documents(
        self,
        nodes: Sequence[BaseNode],
        allow_update: bool = True,
        batch_size: Optional[int] = None,
        store_text: bool = True,
    ) -> None:
        """Add a document to the store.

        Args:
            docs (List[BaseDocument]): documents
            allow_update (bool): allow update of docstore from document

        """
        batch_size = batch_size or self._batch_size

        node_kv_pairs = []
        metadata_kv_pairs = []
        ref_doc_kv_pairs: Dict[str, List[Tuple[str, dict]]] = {}

        for node in nodes:
            # NOTE: doc could already exist in the store, but we overwrite it
            if not allow_update and await self.adocument_exists(node.node_id):
                raise ValueError(
                    f"node_id {node.node_id} already exists. "
                    "Set allow_update to True to overwrite."
                )
            ref_doc_info = None
            if isinstance(node, TextNode) and node.ref_doc_id is not None:
                ref_doc_info = (
                    await self.aget_ref_doc_info(node.ref_doc_id) or RefDocInfo()
                )

            (
                node_kv_pair,
                metadata_kv_pair,
                ref_doc_kv_pair,
            ) = self._get_kv_pairs_for_insert(node, ref_doc_info, store_text)

            if node_kv_pair is not None:
                node_kv_pairs.append(node_kv_pair)
            if metadata_kv_pair is not None:
                metadata_kv_pairs.append(metadata_kv_pair)
            if ref_doc_kv_pair is not None:
                key = ref_doc_kv_pair[0]
                if key not in ref_doc_kv_pairs:
                    ref_doc_kv_pairs[key] = []
                ref_doc_kv_pairs[key].append(ref_doc_kv_pair)

        await self._kvstore.aput_all(
            node_kv_pairs,
            collection=self._node_collection,
            batch_size=batch_size,
        )
        await self._kvstore.aput_all(
            metadata_kv_pairs,
            collection=self._metadata_collection,
            batch_size=batch_size,
        )

        # multiple nodes can point to the same ref_doc_id
        merged_ref_doc_kv_pairs = self._merge_ref_doc_kv_pairs(ref_doc_kv_pairs)
        await self._kvstore.aput_all(
            merged_ref_doc_kv_pairs,
            collection=self._ref_doc_collection,
            batch_size=batch_size,
        )

    def get_document(self, doc_id: str, raise_error: bool = True) -> Optional[BaseNode]:
        """Get a document from the store.

        Args:
            doc_id (str): document id
            raise_error (bool): raise error if doc_id not found

        """
        json = self._kvstore.get(doc_id, collection=self._node_collection)
        if json is None:
            if raise_error:
                raise ValueError(f"doc_id {doc_id} not found.")
            else:
                return None
        return json_to_doc(json)

    async def aget_document(
        self, doc_id: str, raise_error: bool = True
    ) -> Optional[BaseNode]:
        """Get a document from the store.

        Args:
            doc_id (str): document id
            raise_error (bool): raise error if doc_id not found

        """
        json = await self._kvstore.aget(doc_id, collection=self._node_collection)
        if json is None:
            if raise_error:
                raise ValueError(f"doc_id {doc_id} not found.")
            else:
                return None
        return json_to_doc(json)

    def _remove_legacy_info(self, ref_doc_info_dict: dict) -> RefDocInfo:
        if "doc_ids" in ref_doc_info_dict:
            ref_doc_info_dict["node_ids"] = ref_doc_info_dict.get("doc_ids", [])
            ref_doc_info_dict.pop("doc_ids")

            ref_doc_info_dict["metadata"] = ref_doc_info_dict.get("extra_info", {})
            ref_doc_info_dict.pop("extra_info")

        return RefDocInfo(**ref_doc_info_dict)

    def get_ref_doc_info(self, ref_doc_id: str) -> Optional[RefDocInfo]:
        """Get the RefDocInfo for a given ref_doc_id."""
        ref_doc_info = self._kvstore.get(
            ref_doc_id, collection=self._ref_doc_collection
        )
        if not ref_doc_info:
            return None

        # TODO: deprecated legacy support
        return self._remove_legacy_info(ref_doc_info)

    async def aget_ref_doc_info(self, ref_doc_id: str) -> Optional[RefDocInfo]:
        """Get the RefDocInfo for a given ref_doc_id."""
        ref_doc_info = await self._kvstore.aget(
            ref_doc_id, collection=self._ref_doc_collection
        )
        if not ref_doc_info:
            return None

        # TODO: deprecated legacy support
        return self._remove_legacy_info(ref_doc_info)

    def get_all_ref_doc_info(self) -> Optional[Dict[str, RefDocInfo]]:
        """Get a mapping of ref_doc_id -> RefDocInfo for all ingested documents."""
        ref_doc_infos = self._kvstore.get_all(collection=self._ref_doc_collection)
        if ref_doc_infos is None:
            return None

        # TODO: deprecated legacy support
        all_ref_doc_infos = {}
        for doc_id, ref_doc_info in ref_doc_infos.items():
            all_ref_doc_infos[doc_id] = self._remove_legacy_info(ref_doc_info)

        return all_ref_doc_infos

    async def aget_all_ref_doc_info(self) -> Optional[Dict[str, RefDocInfo]]:
        """Get a mapping of ref_doc_id -> RefDocInfo for all ingested documents."""
        ref_doc_infos = await self._kvstore.aget_all(
            collection=self._ref_doc_collection
        )
        if ref_doc_infos is None:
            return None

        # TODO: deprecated legacy support
        all_ref_doc_infos = {}
        for doc_id, ref_doc_info in ref_doc_infos.items():
            all_ref_doc_infos[doc_id] = self._remove_legacy_info(ref_doc_info)
        return all_ref_doc_infos

    def ref_doc_exists(self, ref_doc_id: str) -> bool:
        """Check if a ref_doc_id has been ingested."""
        return self.get_ref_doc_info(ref_doc_id) is not None

    async def aref_doc_exists(self, ref_doc_id: str) -> bool:
        """Check if a ref_doc_id has been ingested."""
        return await self.aget_ref_doc_info(ref_doc_id) is not None

    def document_exists(self, doc_id: str) -> bool:
        """Check if document exists."""
        return self._kvstore.get(doc_id, self._node_collection) is not None

    async def adocument_exists(self, doc_id: str) -> bool:
        """Check if document exists."""
        return await self._kvstore.aget(doc_id, self._node_collection) is not None

    def _remove_ref_doc_node(self, doc_id: str) -> None:
        """Helper function to remove node doc_id from ref_doc_collection."""
        metadata = self._kvstore.get(doc_id, collection=self._metadata_collection)
        if metadata is None:
            return

        ref_doc_id = metadata.get("ref_doc_id", None)

        if ref_doc_id is None:
            return

        ref_doc_info = self._kvstore.get(
            ref_doc_id, collection=self._ref_doc_collection
        )

        if ref_doc_info is not None:
            ref_doc_obj = RefDocInfo(**ref_doc_info)

            ref_doc_obj.node_ids.remove(doc_id)

            # delete ref_doc from collection if it has no more doc_ids
            if len(ref_doc_obj.node_ids) > 0:
                self._kvstore.put(
                    ref_doc_id,
                    ref_doc_obj.to_dict(),
                    collection=self._ref_doc_collection,
                )

            self._kvstore.delete(ref_doc_id, collection=self._metadata_collection)

    async def _aremove_ref_doc_node(self, doc_id: str) -> None:
        """Helper function to remove node doc_id from ref_doc_collection."""
        metadata = await self._kvstore.aget(
            doc_id, collection=self._metadata_collection
        )
        if metadata is None:
            return

        ref_doc_id = metadata.get("ref_doc_id", None)

        if ref_doc_id is None:
            return

        ref_doc_info = await self._kvstore.aget(
            ref_doc_id, collection=self._ref_doc_collection
        )

        if ref_doc_info is not None:
            ref_doc_obj = RefDocInfo(**ref_doc_info)

            ref_doc_obj.node_ids.remove(doc_id)

            # delete ref_doc from collection if it has no more doc_ids
            if len(ref_doc_obj.node_ids) > 0:
                await self._kvstore.aput(
                    ref_doc_id,
                    ref_doc_obj.to_dict(),
                    collection=self._ref_doc_collection,
                )

            await self._kvstore.adelete(
                ref_doc_id, collection=self._metadata_collection
            )

    def delete_document(
        self, doc_id: str, raise_error: bool = True, remove_ref_doc_node: bool = True
    ) -> None:
        """Delete a document from the store."""
        if remove_ref_doc_node:
            self._remove_ref_doc_node(doc_id)

        delete_success = self._kvstore.delete(doc_id, collection=self._node_collection)
        _ = self._kvstore.delete(doc_id, collection=self._metadata_collection)

        if not delete_success and raise_error:
            raise ValueError(f"doc_id {doc_id} not found.")

    async def adelete_document(
        self, doc_id: str, raise_error: bool = True, remove_ref_doc_node: bool = True
    ) -> None:
        """Delete a document from the store."""
        if remove_ref_doc_node:
            await self._aremove_ref_doc_node(doc_id)

        delete_success = await self._kvstore.adelete(
            doc_id, collection=self._node_collection
        )
        _ = await self._kvstore.adelete(doc_id, collection=self._metadata_collection)

        if not delete_success and raise_error:
            raise ValueError(f"doc_id {doc_id} not found.")

    def delete_ref_doc(self, ref_doc_id: str, raise_error: bool = True) -> None:
        """Delete a ref_doc and all it's associated nodes."""
        ref_doc_info = self.get_ref_doc_info(ref_doc_id)
        if ref_doc_info is None:
            if raise_error:
                raise ValueError(f"ref_doc_id {ref_doc_id} not found.")
            else:
                return

        for doc_id in ref_doc_info.node_ids:
            self.delete_document(doc_id, raise_error=False, remove_ref_doc_node=False)

        self._kvstore.delete(ref_doc_id, collection=self._metadata_collection)
        self._kvstore.delete(ref_doc_id, collection=self._ref_doc_collection)

    async def adelete_ref_doc(self, ref_doc_id: str, raise_error: bool = True) -> None:
        """Delete a ref_doc and all it's associated nodes."""
        ref_doc_info = await self.aget_ref_doc_info(ref_doc_id)
        if ref_doc_info is None:
            if raise_error:
                raise ValueError(f"ref_doc_id {ref_doc_id} not found.")
            else:
                return

        for doc_id in ref_doc_info.node_ids:
            await self.adelete_document(
                doc_id, raise_error=False, remove_ref_doc_node=False
            )

        await self._kvstore.adelete(ref_doc_id, collection=self._metadata_collection)
        await self._kvstore.adelete(ref_doc_id, collection=self._ref_doc_collection)

    def set_document_hash(self, doc_id: str, doc_hash: str) -> None:
        """Set the hash for a given doc_id."""
        metadata = {"doc_hash": doc_hash}
        self._kvstore.put(doc_id, metadata, collection=self._metadata_collection)

    def set_document_hashes(self, doc_hashes: Dict[str, str]) -> None:
        """Set the hash for a given doc_id."""
        metadata_kv_pairs = []
        for doc_id, doc_hash in doc_hashes.items():
            metadata_kv_pairs.append((doc_id, {"doc_hash": doc_hash}))

        self._kvstore.put_all(
            metadata_kv_pairs,
            collection=self._metadata_collection,
            batch_size=self._batch_size,
        )

    async def aset_document_hash(self, doc_id: str, doc_hash: str) -> None:
        """Set the hash for a given doc_id."""
        metadata = {"doc_hash": doc_hash}
        await self._kvstore.aput(doc_id, metadata, collection=self._metadata_collection)

    async def aset_document_hashes(self, doc_hashes: Dict[str, str]) -> None:
        """Set the hash for a given doc_id."""
        metadata_kv_pairs = []
        for doc_id, doc_hash in doc_hashes.items():
            metadata_kv_pairs.append((doc_id, {"doc_hash": doc_hash}))

        await self._kvstore.aput_all(
            metadata_kv_pairs,
            collection=self._metadata_collection,
            batch_size=self._batch_size,
        )

    def get_document_hash(self, doc_id: str) -> Optional[str]:
        """Get the stored hash for a document, if it exists."""
        metadata = self._kvstore.get(doc_id, collection=self._metadata_collection)
        if metadata is not None:
            return metadata.get("doc_hash", None)
        else:
            return None

    async def aget_document_hash(self, doc_id: str) -> Optional[str]:
        """Get the stored hash for a document, if it exists."""
        metadata = await self._kvstore.aget(
            doc_id, collection=self._metadata_collection
        )
        if metadata is not None:
            return metadata.get("doc_hash", None)
        else:
            return None

    def get_all_document_hashes(self) -> Dict[str, str]:
        """Get the stored hash for all documents."""
        hashes = {}
        for doc_id in self._kvstore.get_all(collection=self._metadata_collection):
            hash = self.get_document_hash(doc_id)
            if hash is not None:
                hashes[hash] = doc_id
        return hashes

    async def aget_all_document_hashes(self) -> Dict[str, str]:
        """Get the stored hash for all documents."""
        hashes = {}
        for doc_id in await self._kvstore.aget_all(
            collection=self._metadata_collection
        ):
            hash = await self.aget_document_hash(doc_id)
            if hash is not None:
                hashes[hash] = doc_id
        return hashes
