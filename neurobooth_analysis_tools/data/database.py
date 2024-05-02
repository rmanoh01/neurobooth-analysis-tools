import pandas as pd
import numpy as np
import sqlalchemy
from typing import NamedTuple, List, Dict
from time import sleep

from neurobooth_analysis_tools.data.types import DataException


class DatabaseException(DataException):
    """Exception for database-related errors."""
    def __init__(self, *args):
        super(DataException, self).__init__(*args)


class DatabaseConnectionInfo(NamedTuple):
    """Structure containing all the information necessary to connect to a database."""
    user: str
    password: str
    host: str
    port: int
    dbname: str

    def postgresql_url(self):
        return f'postgresql+psycopg2://{self.user}:{self.password}@{self.host}:{self.port}/{self.dbname}'


class DatabaseConnection:
    """Load and provide easy access to Neuropheno subject demographics and clinical information."""
    subject: pd.DataFrame = None
    session: pd.DataFrame = None
    demographic: pd.DataFrame = None
    clinical: pd.DataFrame = None
    scales: pd.DataFrame = None
    prom_vaq: pd.DataFrame = None
    prom_ataxia: pd.DataFrame = None
    test_subjects: np.ndarray = None

    def __init__(self, connection_info: DatabaseConnectionInfo):
        """Create an object that can connect to the database, run queries or download tables, and cache results."""
        self.connection_info = connection_info
        self.engine = sqlalchemy.create_engine(self.connection_info.postgresql_url())

    def download(self) -> None:
        """Download tables likely to be useful for analysis and fuzzy-join them with sessions by date."""
        tables = self.download_tables(
            'subject',
            'rc_visit_dates',
            'rc_demographic_clean',
            'rc_clinical_clean',
            'rc_ataxia_pd_scales_clean',
            'rc_visual_activities_questionnaire',
            'rc_prom_ataxia',
        )
        self.subject = tables.pop('subject')
        self.session = tables.pop('rc_visit_dates')
        session_view = self.session[['subject_id', 'redcap_event_name', 'neurobooth_visit_dates']]

        join_keys = {
            'rc_demographic_clean': 'end_time_demographic',
            'rc_clinical_clean': 'end_time_clinical',
            'rc_ataxia_pd_scales_clean': 'visit_date',
            'rc_visual_activities_questionnaire': 'end_time_visual_activities_questionnaire',
            'rc_prom_ataxia': 'end_time_prom_ataxia',
        }
        new_column_prefix = {
            'rc_demographic_clean': 'demographic',
            'rc_clinical_clean': 'clinical',
            'rc_ataxia_pd_scales_clean': 'scales',
            'rc_visual_activities_questionnaire': 'vaq',
            'rc_prom_ataxia': 'prom_ataxia',
        }

        tables = {
            name: fuzzy_join_date(
                session_view, table,
                hard_on=['subject_id'], fuzzy_on_left='neurobooth_visit_dates', fuzzy_on_right=join_keys[name],
                offset_column_name=f'{new_column_prefix[name]}_offset_days',
                how='left'
            )
            for name, table in tables.items()
        }
        self.demographic = tables['rc_demographic_clean']
        self.clinical = tables['rc_clinical_clean']
        self.scales = tables['rc_ataxia_pd_scales_clean']
        self.prom_vaq = tables['rc_visual_activities_questionnaire']
        self.prom_ataxia = tables['rc_prom_ataxia']

    def download_tables(self, *table_names: str) -> Dict[str, pd.DataFrame]:
        """Download and return the specified tables from the database"""
        DatabaseConnection.wait_for_refresh(self.engine, *table_names)
        with self.engine.connect() as connection:
            return {
                table_name: pd.read_sql_table(table_name, connection).convert_dtypes()
                for table_name in table_names
            }

    @staticmethod
    def wait_for_refresh(
            engine: sqlalchemy.engine.Engine,
            *table_names: str,
            max_polls: int = 6,
            poll_interval_sec: float = 5,
    ) -> None:
        """
        The Neurobooth database is recreated on an hourly basis.
        This method checks if the all tables exist. If not, it will block and periodically recheck until either the
        tables exist or the maximum number of checks is reached.
        """
        for _ in range(max_polls):
            inspection = sqlalchemy.inspect(engine)
            exists = all([inspection.has_table(table) for table in table_names])
            if exists:
                return
            else:
                sleep(poll_interval_sec)

        raise DatabaseException(
            f"Exceeded maximum polls (N={max_polls}) when checking for existence of: {', '.join(table_names)}."
        )

    def get_test_subjects(self, *, use_cache: bool = True) -> np.ndarray:
        """
        Determine which subject IDs correspond to test subjects based on the database.
        :param use_cache: Whether to used cached results (if available) or re-query the database.
        :return: An array of subject IDs that are test subjects.
        """
        if use_cache and self.test_subjects is not None:
            return self.test_subjects

        # Test subjects will either be:
        #   1) missing from the redcap-generated consent table (but present in the subject table), or
        #   2) flagged as a test subject in the consent table
        query = '''
        SELECT DISTINCT subj.subject_id
        FROM subject subj
        LEFT JOIN rc_participant_and_consent_information pci
            ON subj.subject_id = pci.subject_id
        WHERE pci.test_subject_boolean OR pci.subject_id IS NULL
        ORDER BY subj.subject_id
        '''

        DatabaseConnection.wait_for_refresh(self.engine, 'rc_participant_and_consent_information')
        with self.engine.connect() as connection:
            self.test_subjects = pd.read_sql(query, connection).convert_dtypes().to_numpy(dtype='U').squeeze()
        return self.test_subjects


def fuzzy_join_date(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        hard_on: List[str],
        fuzzy_on_left: str,
        fuzzy_on_right: str,
        offset_column_name: str = 'Offset_Days',
        **kwargs,
) -> pd.DataFrame:
    """
    Do a fuzzy join based on two date columns, where the closest match between the dates is selected.

    :param left_df: The left dataframe in the join
    :param right_df: The right dataframe in the join
    :param hard_on: Any non-fuzzy join conditions
    :param fuzzy_on_left: The date column to be used in the left dataframe
    :param fuzzy_on_right:  The date column to be used in the right dataframe
    :param offset_column_name:  The name of the column that will contain the calculated date offset
    :param kwargs: Any kwargs that should be passed on to the join (e.g., 'how' to specify join type)
    :return: The joined dataframe, with an added column for the separation of the joined dates.
    """
    possible_matches = pd.merge(left_df, right_df, on=hard_on, **kwargs)

    # Calculate number of days (signed) between each date column in the fuzzy join
    possible_matches[offset_column_name] = possible_matches[fuzzy_on_right] - possible_matches[fuzzy_on_left]
    possible_matches[offset_column_name] /= np.timedelta64(1, 'D')  # Convert to days

    # Rank possible matches based on proximity
    possible_matches['__Rank__'] = possible_matches[offset_column_name].abs()
    possible_matches['__Rank__'] = possible_matches \
        .groupby([*hard_on, fuzzy_on_left])['__Rank__'] \
        .rank(method='min', na_option='bottom')

    # Only keep the best matches (rank 1)
    mask = possible_matches['__Rank__'] == 1
    possible_matches = possible_matches.loc[mask]
    return possible_matches.drop(columns='__Rank__')  # No longer needed
