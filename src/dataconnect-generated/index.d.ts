import { ConnectorConfig, DataConnect, QueryRef, QueryPromise, ExecuteQueryOptions, MutationRef, MutationPromise } from 'firebase/data-connect';

export const connectorConfig: ConnectorConfig;

export type TimestampString = string;
export type UUIDString = string;
export type Int64String = string;
export type DateString = string;




export interface Basket_Key {
  id: UUIDString;
  __typename?: 'Basket_Key';
}

export interface CreateMyBasketData {
  basket_insert: Basket_Key;
}

export interface CreateMyBasketVariables {
  name: string;
  symbol: string;
  enabled: boolean;
  configuration: string;
}

export interface DeleteMyBasketData {
  basket_delete?: Basket_Key | null;
}

export interface DeleteMyBasketVariables {
  id: UUIDString;
}

export interface GetMyProfileData {
  userProfiles: ({
    uid: string;
    email?: string | null;
    displayName?: string | null;
    role: string;
    isActive: boolean;
    createdAt: TimestampString;
    updatedAt: TimestampString;
  } & UserProfile_Key)[];
}

export interface GetMyRiskSettingsData {
  riskSettingss: ({
    ownerUid: string;
    riskProfile: string;
    maxPortfolioDrawdownPct: number;
    maxGrossLeverage: number;
    maxMarginUtilizationPct: number;
    updatedAt: TimestampString;
  } & RiskSettings_Key)[];
}

export interface ListMyBasketsData {
  baskets: ({
    id: UUIDString;
    ownerUid: string;
    name: string;
    symbol: string;
    enabled: boolean;
    configuration: string;
    createdAt: TimestampString;
    updatedAt: TimestampString;
  } & Basket_Key)[];
}

export interface RiskSettings_Key {
  ownerUid: string;
  __typename?: 'RiskSettings_Key';
}

export interface SetUserProfileAccessData {
  userProfile_upsert: UserProfile_Key;
}

export interface SetUserProfileAccessVariables {
  uid: string;
  role: string;
  isActive: boolean;
}

export interface UpdateMyBasketData {
  basket_update?: Basket_Key | null;
}

export interface UpdateMyBasketVariables {
  id: UUIDString;
  name: string;
  symbol: string;
  enabled: boolean;
  configuration: string;
}

export interface UpdateMyProfileData {
  userProfile_update?: UserProfile_Key | null;
}

export interface UpdateMyProfileVariables {
  email?: string | null;
  displayName?: string | null;
}

export interface UpsertMyRiskSettingsData {
  riskSettings_upsert: RiskSettings_Key;
}

export interface UpsertMyRiskSettingsVariables {
  riskProfile: string;
  maxPortfolioDrawdownPct: number;
  maxGrossLeverage: number;
  maxMarginUtilizationPct: number;
}

export interface UserProfile_Key {
  uid: string;
  __typename?: 'UserProfile_Key';
}

interface GetMyProfileRef {
  /* Allow users to create refs without passing in DataConnect */
  (): QueryRef<GetMyProfileData, undefined>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect): QueryRef<GetMyProfileData, undefined>;
  operationName: string;
}
export const getMyProfileRef: GetMyProfileRef;

export function getMyProfile(options?: ExecuteQueryOptions): QueryPromise<GetMyProfileData, undefined>;
export function getMyProfile(dc: DataConnect, options?: ExecuteQueryOptions): QueryPromise<GetMyProfileData, undefined>;

interface UpdateMyProfileRef {
  /* Allow users to create refs without passing in DataConnect */
  (vars?: UpdateMyProfileVariables): MutationRef<UpdateMyProfileData, UpdateMyProfileVariables>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect, vars?: UpdateMyProfileVariables): MutationRef<UpdateMyProfileData, UpdateMyProfileVariables>;
  operationName: string;
}
export const updateMyProfileRef: UpdateMyProfileRef;

export function updateMyProfile(vars?: UpdateMyProfileVariables): MutationPromise<UpdateMyProfileData, UpdateMyProfileVariables>;
export function updateMyProfile(dc: DataConnect, vars?: UpdateMyProfileVariables): MutationPromise<UpdateMyProfileData, UpdateMyProfileVariables>;

interface SetUserProfileAccessRef {
  /* Allow users to create refs without passing in DataConnect */
  (vars: SetUserProfileAccessVariables): MutationRef<SetUserProfileAccessData, SetUserProfileAccessVariables>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect, vars: SetUserProfileAccessVariables): MutationRef<SetUserProfileAccessData, SetUserProfileAccessVariables>;
  operationName: string;
}
export const setUserProfileAccessRef: SetUserProfileAccessRef;

export function setUserProfileAccess(vars: SetUserProfileAccessVariables): MutationPromise<SetUserProfileAccessData, SetUserProfileAccessVariables>;
export function setUserProfileAccess(dc: DataConnect, vars: SetUserProfileAccessVariables): MutationPromise<SetUserProfileAccessData, SetUserProfileAccessVariables>;

interface ListMyBasketsRef {
  /* Allow users to create refs without passing in DataConnect */
  (): QueryRef<ListMyBasketsData, undefined>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect): QueryRef<ListMyBasketsData, undefined>;
  operationName: string;
}
export const listMyBasketsRef: ListMyBasketsRef;

export function listMyBaskets(options?: ExecuteQueryOptions): QueryPromise<ListMyBasketsData, undefined>;
export function listMyBaskets(dc: DataConnect, options?: ExecuteQueryOptions): QueryPromise<ListMyBasketsData, undefined>;

interface CreateMyBasketRef {
  /* Allow users to create refs without passing in DataConnect */
  (vars: CreateMyBasketVariables): MutationRef<CreateMyBasketData, CreateMyBasketVariables>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect, vars: CreateMyBasketVariables): MutationRef<CreateMyBasketData, CreateMyBasketVariables>;
  operationName: string;
}
export const createMyBasketRef: CreateMyBasketRef;

export function createMyBasket(vars: CreateMyBasketVariables): MutationPromise<CreateMyBasketData, CreateMyBasketVariables>;
export function createMyBasket(dc: DataConnect, vars: CreateMyBasketVariables): MutationPromise<CreateMyBasketData, CreateMyBasketVariables>;

interface UpdateMyBasketRef {
  /* Allow users to create refs without passing in DataConnect */
  (vars: UpdateMyBasketVariables): MutationRef<UpdateMyBasketData, UpdateMyBasketVariables>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect, vars: UpdateMyBasketVariables): MutationRef<UpdateMyBasketData, UpdateMyBasketVariables>;
  operationName: string;
}
export const updateMyBasketRef: UpdateMyBasketRef;

export function updateMyBasket(vars: UpdateMyBasketVariables): MutationPromise<UpdateMyBasketData, UpdateMyBasketVariables>;
export function updateMyBasket(dc: DataConnect, vars: UpdateMyBasketVariables): MutationPromise<UpdateMyBasketData, UpdateMyBasketVariables>;

interface DeleteMyBasketRef {
  /* Allow users to create refs without passing in DataConnect */
  (vars: DeleteMyBasketVariables): MutationRef<DeleteMyBasketData, DeleteMyBasketVariables>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect, vars: DeleteMyBasketVariables): MutationRef<DeleteMyBasketData, DeleteMyBasketVariables>;
  operationName: string;
}
export const deleteMyBasketRef: DeleteMyBasketRef;

export function deleteMyBasket(vars: DeleteMyBasketVariables): MutationPromise<DeleteMyBasketData, DeleteMyBasketVariables>;
export function deleteMyBasket(dc: DataConnect, vars: DeleteMyBasketVariables): MutationPromise<DeleteMyBasketData, DeleteMyBasketVariables>;

interface GetMyRiskSettingsRef {
  /* Allow users to create refs without passing in DataConnect */
  (): QueryRef<GetMyRiskSettingsData, undefined>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect): QueryRef<GetMyRiskSettingsData, undefined>;
  operationName: string;
}
export const getMyRiskSettingsRef: GetMyRiskSettingsRef;

export function getMyRiskSettings(options?: ExecuteQueryOptions): QueryPromise<GetMyRiskSettingsData, undefined>;
export function getMyRiskSettings(dc: DataConnect, options?: ExecuteQueryOptions): QueryPromise<GetMyRiskSettingsData, undefined>;

interface UpsertMyRiskSettingsRef {
  /* Allow users to create refs without passing in DataConnect */
  (vars: UpsertMyRiskSettingsVariables): MutationRef<UpsertMyRiskSettingsData, UpsertMyRiskSettingsVariables>;
  /* Allow users to pass in custom DataConnect instances */
  (dc: DataConnect, vars: UpsertMyRiskSettingsVariables): MutationRef<UpsertMyRiskSettingsData, UpsertMyRiskSettingsVariables>;
  operationName: string;
}
export const upsertMyRiskSettingsRef: UpsertMyRiskSettingsRef;

export function upsertMyRiskSettings(vars: UpsertMyRiskSettingsVariables): MutationPromise<UpsertMyRiskSettingsData, UpsertMyRiskSettingsVariables>;
export function upsertMyRiskSettings(dc: DataConnect, vars: UpsertMyRiskSettingsVariables): MutationPromise<UpsertMyRiskSettingsData, UpsertMyRiskSettingsVariables>;
